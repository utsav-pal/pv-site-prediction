import datetime as dt
import importlib
import logging

import click
import numpy as np
import pandas as pd
import torch
import tqdm

from psp.dataset import pv_list_to_short_str
from psp.exp_configs.base import ExpConfigBase
from psp.metrics import Metric, mean_absolute_error
from psp.models.multi import MultiPvSiteModel
from psp.scripts._options import exp_name_opt, exp_root_opt, log_level_opt, num_workers_opt
from psp.serialization import load_model
from psp.training import make_data_loader
from psp.utils.interupting import continue_on_interupt

METRICS: dict[str, Metric] = {
    # "mre_cap=1": MeanRelativeError(cap=1),
    "mae": mean_absolute_error,
}

_log = logging.getLogger(__name__)


@click.command()
@exp_root_opt
@exp_name_opt
@num_workers_opt
@log_level_opt
@click.option(
    "-l",
    "--limit",
    type=int,
    default=1000,
    help="Maximum number of samples to consider.",
)
@click.option(
    "--split", "split_name", type=str, default="test", help="split of the data to use: train | test"
)
def main(exp_root, exp_name, num_workers, limit, split_name, log_level):
    logging.basicConfig(level=getattr(logging, log_level.upper()))

    assert split_name in ["train", "test"]
    # This fixes problems when loading files in parallel on GCP.
    # https://pytorch.org/docs/stable/notes/multiprocessing.html#cuda-in-multiprocessing
    # https://github.com/fsspec/gcsfs/issues/379
    torch.multiprocessing.set_start_method("spawn")

    exp_config_module = importlib.import_module(".config", f"{exp_root}.{exp_name}")
    exp_config: ExpConfigBase = exp_config_module.ExpConfig()

    data_source_kwargs = exp_config.get_data_source_kwargs()
    pv_data_source = exp_config.get_pv_data_source()

    # Those are the dates we trained models for.
    dates_split = exp_config.get_date_splits()
    train_dates = dates_split.train_dates

    # Load the saved models.
    model_list = [
        load_model(exp_root / exp_name / f"model_{i}.pkl") for i in range(len(train_dates))
    ]
    models = {date: model for date, model in zip(train_dates, model_list)}
    # Wrap them into one big meta model.
    model = MultiPvSiteModel(models)

    model.set_data_sources(**data_source_kwargs)

    # We can start testing after the earliest one.
    test_start = min(train_dates) + dt.timedelta(days=1)

    test_end = test_start + dt.timedelta(days=dates_split.num_test_days)

    model_config = exp_config.get_model_config()

    # Setup the dataset.

    # TODO make sure the train_split from the model is consistent with the test one - we could
    # save in the model details about the training and check them here.
    pv_splits = exp_config.make_pv_splits(pv_data_source)
    pv_ids = getattr(pv_splits, split_name)

    _log.info(f"Evaluating on split: {pv_list_to_short_str(pv_ids)}")

    random_state = np.random.RandomState(1234)

    # Use a torch DataLoader to create samples efficiently.
    data_loader = make_data_loader(
        data_source=pv_data_source,
        horizons=model_config.horizons,
        pv_ids=pv_ids,
        start_ts=test_start,
        end_ts=test_end,
        batch_size=None,
        random_state=random_state,
        prob_keep_sample=1.0,
        get_features=model.get_features,
        num_workers=num_workers,
        shuffle=True,
        step=15,
        limit=limit,
    )

    # Gather all errors for every samples. We'll make a DataFrame with it.
    error_rows = []

    pv_data_has_capacity = "capacity" in pv_data_source.list_data_variables()

    with continue_on_interupt(prompt=False):
        for i, sample in tqdm.tqdm(enumerate(data_loader), total=limit):
            x = sample.x

            extra = {}

            if pv_data_has_capacity:
                capacity = pv_data_source.get(
                    pv_ids=x.pv_id, start_ts=x.ts - dt.timedelta(days=7), end_ts=x.ts
                )["capacity"].values[-1]
                extra["capacity"] = capacity

            y_true = sample.y
            y_pred = model.predict_from_features(x=x, features=sample.features)
            for metric_name, metric in METRICS.items():
                error = metric(y_true, y_pred)
                # Error is a vector
                for i, (err_value, y, pred) in enumerate(zip(error, y_true.powers, y_pred.powers)):
                    horizon = model_config.horizons[i][0]
                    error_rows.append(
                        {
                            "pv_id": x.pv_id,
                            "ts": x.ts,
                            "metric": metric_name,
                            "error": err_value,
                            "horizon": horizon,
                            "y": y,
                            "pred": pred,
                            **extra,
                        }
                    )

    df = pd.DataFrame.from_records(error_rows)

    exp_name = exp_name or dt.datetime.now().isoformat()

    output_dir = exp_root / exp_name
    print(f"Saving results to {output_dir}")
    output_dir.mkdir(exist_ok=True)
    df.to_csv(output_dir / f"{split_name}_errors.csv")


if __name__ == "__main__":
    main()
