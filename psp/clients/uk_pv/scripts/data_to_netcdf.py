import click
import pandas as pd
import xarray as xr

from psp.clients.uk_pv.data import C


@click.command()
@click.argument("input", type=click.Path(exists=True), required=True)
@click.argument("output", type=click.Path(exists=False), required=True)
@click.option(
    "--meta",
    "-m",
    multiple=True,
    help="Meta data to join. This argument can be used many times, the last files have precedence.",
)
@click.option(
    "--power-conversion-factor",
    default=1.0,
    help="If the power units are not kW, provide a factor by which to multiply the values to"
    " convert them. Note that we assume that the metadata is already in the right units.",
    show_default=True,
)
def main(input, output, meta, power_conversion_factor: float):
    """Convert a .parquet INPUT file to a .xarray file.

    NOTE that the .parquet file must have been generated by simplify_data.py.
    """
    print("Read meta data")
    metas = meta
    metas = [pd.read_csv(m).set_index(C.id).sort_index() for m in metas]

    print("Load parquet file")
    df = pd.read_parquet(input)

    print(df.head())

    print("Convert to xarray dataset")
    ds = xr.Dataset.from_dataframe(
        df,
    )

    if power_conversion_factor != 1.0:
        print("Convert units")
        ds = ds * power_conversion_factor

    # Keep only the ss_ids that have data in all the meta files.
    # This way a given column always come only from one file.
    print("Find ss_ids that are both in the `data` and in `meta`")
    ss_ids_set = set(list(ds.coords["ss_id"].values))
    for m in metas:
        ss_ids_set = ss_ids_set & set(m.index.to_list())
    ss_ids = list(ss_ids_set)

    metas = [m.loc[ss_ids] for m in metas]

    # Filter the intersection of ss_ids in the dataset.
    ds = ds.sel(ss_id=ss_ids)

    print("Add coords to dataset")
    columns = {"orientation", "tilt", "factor", "latitude", "longitude", "kwp"}

    for meta in metas:
        ds = ds.assign_coords(
            {
                name: ([C.id], meta.loc[ss_ids, name])
                # Only consider a subset of columns.
                for name in set(meta.columns) & columns
            }
        )

    print("Save")
    ds.to_netcdf(output)


if __name__ == "__main__":
    main()
