import importlib
import logging

import click
import numpy as np
import torch

from psp.dataset import split_train_test
from psp.scripts._options import (
    exp_config_opt,
    exp_name_opt,
    exp_root_opt,
    num_workers_opt,
)
from psp.serialization import save_model
from psp.training import make_data_loader

_log = logging.getLogger(__name__)


@click.command()
@exp_root_opt
@exp_name_opt
@exp_config_opt
@num_workers_opt
@click.option("-b", "--batch-size", default=32)
@click.option("--log-level", type=str, help="Debug level", default="info", show_default=True)
def main(exp_root, exp_name, exp_config_name, num_workers, batch_size, log_level: str):
    logging.basicConfig(level=getattr(logging, log_level.upper()))

    # This fixes problems when loading files in parallel on GCP.
    # https://pytorch.org/docs/stable/notes/multiprocessing.html#cuda-in-multiprocessing
    # https://github.com/fsspec/gcsfs/issues/379
    torch.multiprocessing.set_start_method("spawn")

    exp_config_module = importlib.import_module("." + exp_config_name, "psp.exp_configs")
    exp_config = exp_config_module.ExpConfig()

    random_state = np.random.RandomState(1234)

    # Load the model.
    model = exp_config.get_model()

    pv_data_source = exp_config.get_pv_data_source()

    # Dataset
    splits = split_train_test(pv_data_source)

    _log.info(f"Training on split: {splits.train}")

    train_data_loader = make_data_loader(
        data_source=pv_data_source,
        horizons=model.config.horizons,
        split=splits.train,
        batch_size=batch_size,
        get_features=model.get_features,
        num_workers=num_workers,
        random_state=random_state,
        shuffle=True,
    )

    limit = 128

    # Ensure that way we always have the same valid set, no matter the batch size (for this we need
    # to have only whole batches).
    assert limit % batch_size == 0

    _log.info(f"Validating on split: {splits.valid}")

    valid_data_loader = make_data_loader(
        data_source=pv_data_source,
        horizons=model.config.horizons,
        split=splits.valid,
        batch_size=batch_size,
        get_features=model.get_features,
        num_workers=num_workers,
        random_state=np.random.RandomState(4321),
        # We shuffle to get a good sample of data points.
        shuffle=True,
        limit=limit,
    )

    model.train(train_data_loader, valid_data_loader, batch_size)

    output_dir = exp_root / exp_name
    output_dir.mkdir(exist_ok=True)
    path = output_dir / "model.pkl"
    _log.info(f"Saving model to {path}")
    save_model(model, path)


if __name__ == "__main__":
    main()
