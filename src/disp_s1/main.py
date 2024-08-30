from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from itertools import repeat
from multiprocessing import get_context
from pathlib import Path
from typing import NamedTuple

from dolphin._log import log_runtime, setup_logging
from dolphin.io import load_gdal
from dolphin.utils import DummyProcessPoolExecutor, get_max_memory_usage
from dolphin.workflows.config import DisplacementWorkflow
from dolphin.workflows.displacement import OutputPaths
from dolphin.workflows.displacement import run as run_displacement
from opera_utils import get_dates, group_by_date

from disp_s1 import __version__, product
from disp_s1.pge_runconfig import RunConfig

from ._reference import ReferencePoint, read_reference_point

logger = logging.getLogger(__name__)


@log_runtime
def run(
    cfg: DisplacementWorkflow,
    pge_runconfig: RunConfig,
    debug: bool = False,
):
    """Run the displacement workflow on a stack of SLCs.

    :param cfg: DisplacementWorkflow: 
    :param pge_runconfig: RunConfig: 
    :param debug: bool:  (Default value = False)

    
    """
    setup_logging(logger_name="disp_s1", debug=debug, filename=cfg.log_file)

    # Run dolphin's displacement workflow
    out_paths = run_displacement(cfg=cfg, debug=debug)

    # Create the short wavelength layer for the product
    if hasattr(cfg, "spatial_wavelength_cutoff"):
        wavelength_cutoff = cfg.spatial_wavelength_cutoff
    else:
        wavelength_cutoff = 50_000

    # Read the reference point
    assert out_paths.timeseries_paths is not None
    timeseries_dir = out_paths.timeseries_paths[0].parent
    ref_point = read_reference_point(timeseries_dir)

    # Finalize the output as an HDF5 product
    # Group all the CSLCs by date to pick out ref/secondaries
    date_to_cslc_files = group_by_date(cfg.cslc_file_list, date_idx=0)

    out_dir = pge_runconfig.product_path_group.output_directory
    out_dir.mkdir(exist_ok=True, parents=True)
    logger.info(f"Creating {len(out_paths.timeseries_paths)} outputs in {out_dir}")

    # group dataset based on date to find corresponding files and set None
    # for the layers that do not exist: correction layers specifically
    grouped_unwrapped_paths = group_by_date(out_paths.timeseries_paths)
    unw_date_keys = list(grouped_unwrapped_paths.keys())
    _assert_dates_match(unw_date_keys, out_paths.conncomp_paths, "connected components")
    _assert_dates_match(unw_date_keys, out_paths.stitched_cor_paths, "correlation")

    if out_paths.tropospheric_corrections is not None:
        _assert_dates_match(
            unw_date_keys, out_paths.tropospheric_corrections, "troposphere"
        )

    if out_paths.ionospheric_corrections is not None:
        _assert_dates_match(
            unw_date_keys, out_paths.ionospheric_corrections, "ionosphere"
        )

    logger.info(f"Creating {len(out_paths.timeseries_paths)} outputs in {out_dir}")
    create_displacement_products(
        out_paths,
        out_dir=out_dir,
        date_to_cslc_files=date_to_cslc_files,
        pge_runconfig=pge_runconfig,
        wavelength_cutoff=wavelength_cutoff,
        reference_point=ref_point,
    )
    logger.info("Finished creating output products.")

    if pge_runconfig.product_path_group.save_compressed_slc:
        logger.info(f"Saving {len(out_paths.comp_slc_dict.items())} compressed SLCs")
        output_dir = out_dir / "compressed_slcs"
        output_dir.mkdir(exist_ok=True)
        product.create_compressed_products(
            comp_slc_dict=out_paths.comp_slc_dict,
            output_dir=output_dir,
            cslc_file_list=cfg.cslc_file_list,
        )

    logger.info(f"Product type: {pge_runconfig.primary_executable.product_type}")
    logger.info(f"Product version: {pge_runconfig.product_path_group.product_version}")
    max_mem = get_max_memory_usage(units="GB")
    logger.info(f"Maximum memory usage: {max_mem:.2f} GB")
    logger.info(f"Config file dolphin version: {cfg._dolphin_version}")
    logger.info(f"Current running disp_s1 version: {__version__}")


def _assert_dates_match(
    unw_date_keys: list[datetime], test_paths: list[Path], name: str
):
    """

    :param unw_date_keys: list[datetime]: 
    :param test_paths: list[Path]: 
    :param name: str: 

    """
    if list(group_by_date(test_paths).keys()) != unw_date_keys:
        msg = f"Mismatch of dates found for {name}:"
        msg += f"{unw_date_keys =}, but {name} has {test_paths}"
        raise ValueError(msg)


class ProductFiles(NamedTuple):
    """Named tuple to hold the files for each NetCDF product."""

    unwrapped: Path
    conncomp: Path
    temp_coh: Path
    correlation: Path
    ps_mask: Path
    troposphere: Path | None
    ionosphere: Path | None
    unwrapper_mask: Path | None


def process_product(
    files: ProductFiles,
    out_dir: Path,
    date_to_cslc_files: Mapping[tuple[datetime], list[Path]],
    pge_runconfig: RunConfig,
    wavelength_cutoff: float,
    reference_point: ReferencePoint | None = None,
) -> Path:
    """Create a single displacement product.

    :param files: NamedTuple containing paths for all displacement-related files.
    :type files: ProductFiles
    :param out_dir: Output directory for the product.
    :type out_dir: Path
    :param date_to_cslc_files: Dictionary mapping dates to real/compressed SLC files.
    :type date_to_cslc_files: Mapping[tuple[datetime], list[Path]]
    :param pge_runconfig: Configuration object for the PGE run.
    :type pge_runconfig: RunConfig
    :param wavelength_cutoff: Wavelength cutoff for filtering long wavelengths.
    :type wavelength_cutoff: float
    :param reference_point: Reference point recorded from dolphin after unwrapping.
        If none, leaves product attributes empty.
    :type reference_point: ReferencePoint, optional
    :param files: ProductFiles: 
    :param out_dir: Path: 
    :param date_to_cslc_files: Mapping[tuple[datetime]: 
    :param list[Path]]: 
    :param pge_runconfig: RunConfig: 
    :param wavelength_cutoff: float: 
    :param reference_point: ReferencePoint | None:  (Default value = None)

    
    """
    corrections = {}

    if files.troposphere is not None:
        corrections["troposphere"] = load_gdal(files.troposphere)
    else:
        logger.warning(
            "Missing tropospheric correction for %s. Creating empty layer.",
            files.unwrapped,
        )

    if files.ionosphere is not None:
        corrections["ionosphere"] = load_gdal(files.ionosphere)
    else:
        logger.warning(
            "Missing ionospheric correction for %s. Creating empty layer.",
            files.unwrapped,
        )

    output_name = out_dir / files.unwrapped.with_suffix(".nc").name
    ref_date, secondary_date = get_dates(output_name)[:2]
    # The reference one could be compressed, or real
    # Also possible to have multiple compressed files with same reference date
    ref_slc_files = date_to_cslc_files[(ref_date,)]
    logger.info(f"Found {len(ref_slc_files)} for reference date {ref_date}")
    secondary_slc_files = date_to_cslc_files[(secondary_date,)]
    logger.info(f"Found {len(secondary_slc_files)} for secondary date {secondary_date}")

    product.create_output_product(
        output_name=output_name,
        unw_filename=files.unwrapped,
        conncomp_filename=files.conncomp,
        temp_coh_filename=files.temp_coh,
        ifg_corr_filename=files.correlation,
        ps_mask_filename=files.ps_mask,
        unwrapper_mask_filename=files.unwrapper_mask,
        pge_runconfig=pge_runconfig,
        reference_cslc_file=ref_slc_files[0],
        secondary_cslc_file=secondary_slc_files[0],
        corrections=corrections,
        wavelength_cutoff=wavelength_cutoff,
        reference_point=reference_point,
    )

    return output_name


def create_displacement_products(
    out_paths: OutputPaths,
    out_dir: Path,
    date_to_cslc_files: Mapping[tuple[datetime], list[Path]],
    pge_runconfig: RunConfig,
    wavelength_cutoff: float = 50_000.0,
    reference_point: ReferencePoint | None = None,
    max_workers: int = 2,
) -> None:
    """Run parallel processing for all interferograms.

    :param out_paths: OutputPaths: 
    :param out_dir: Path: 
    :param date_to_cslc_files: Mapping[tuple[datetime]: 
    :param list[Path]]: 
    :param pge_runconfig: RunConfig: 
    :param wavelength_cutoff: float:  (Default value = 50_000.0)
    :param reference_point: ReferencePoint | None:  (Default value = None)
    :param max_workers: int:  (Default value = 2)

    
    """
    tropo_files = out_paths.tropospheric_corrections or [None] * len(
        out_paths.timeseries_paths
    )
    iono_files = out_paths.ionospheric_corrections or [None] * len(
        out_paths.timeseries_paths
    )
    unwrapper_mask_files = [
        # TODO: probably relying too much on dolphin's internals to be safe
        # we should figure out how to save/use the "combined_mask" from dolphin
        Path(str(p).replace(".cor.tif", ".mask.tif"))
        for p in out_paths.stitched_cor_paths
    ]
    files = [
        ProductFiles(
            unwrapped=unw,
            conncomp=cc,
            temp_coh=out_paths.stitched_temp_coh_file,
            correlation=cor,
            ps_mask=out_paths.stitched_ps_file,
            troposphere=tropo,
            ionosphere=iono,
            unwrapper_mask=mask_f,
        )
        for unw, cc, cor, tropo, iono, mask_f in zip(
            out_paths.timeseries_paths,
            out_paths.conncomp_paths,
            out_paths.stitched_cor_paths,
            tropo_files,
            iono_files,
            unwrapper_mask_files,
        )
    ]

    executor_class = (
        ProcessPoolExecutor if max_workers > 1 else DummyProcessPoolExecutor
    )
    ctx = get_context("spawn")
    with executor_class(max_workers=max_workers, mp_context=ctx) as executor:
        list(  # Force evaluation to retrieve results/raise exceptions
            executor.map(
                process_product,
                files,
                repeat(out_dir),
                repeat(date_to_cslc_files),
                repeat(pge_runconfig),
                repeat(wavelength_cutoff),
                repeat(reference_point),
            )
        )
