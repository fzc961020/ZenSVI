import json
import os

import pandas as pd
import pytest

from zensvi.download import MLYDownloader
from zensvi.download.mapillary import interface


@pytest.fixture
def output(base_output_dir, ensure_dir):
    output_dir = base_output_dir / "mly_output"
    ensure_dir(output_dir)
    return output_dir


@pytest.fixture
def mly_api_key():
    api_key = os.getenv("MLY_API_KEY")
    if not api_key:
        pytest.skip("MLY_API_KEY environment variable not set")
    interface.set_access_token(api_key)
    return api_key


@pytest.fixture
def mly_input_files(input_dir):
    return {
        "multipolygon": input_dir / "test_multipolygon_sg.geojson",
        "polygon": input_dir / "test_polygon_sg.geojson",
    }


def test_interface(output, mly_input_files, mly_api_key):
    output_file = output / "test_interface.json"
    if output_file.exists():
        pytest.skip("Result exists")

    with open(mly_input_files["polygon"]) as f:
        geojson = json.load(f)
    output_data = interface.images_in_geojson(geojson)
    assert len(output_data.to_dict()) > 0


@pytest.mark.parametrize(
    "input_type,expected_files",
    [
        ("coordinates", 1),  # lat/lon input
        ("csv", 1),  # CSV file input
        ("polygon", 1),  # Single polygon
        ("multipolygon", 1),  # Multiple polygons
        ("place_name", 1),  # Place name input
    ],
)
def test_downloader_input_types(output, mly_input_files, input_dir, mly_api_key, input_type, expected_files):
    """Test downloading with different input types"""
    output_dir = output / f"test_{input_type}"
    mly_downloader = MLYDownloader(mly_api_key, log_path=output_dir / "log.log", max_workers=1)

    # Set up input parameters based on type
    kwargs = {}
    if input_type == "coordinates":
        kwargs = {"lat": 11.827575599999989, "lon": 13.146558000000027, "buffer": 100}
    elif input_type == "csv":
        test_csv = input_dir / "test_sg.csv"
        if not test_csv.exists():
            pd.DataFrame({"latitude": [11.827575599999989], "longitude": [13.146558000000027]}).to_csv(
                test_csv, index=False
            )
        kwargs = {"input_csv_file": str(test_csv), "buffer": 100}
    elif input_type == "polygon":
        kwargs = {"input_shp_file": str(mly_input_files["polygon"])}
    elif input_type == "multipolygon":
        kwargs = {"input_shp_file": str(mly_input_files["multipolygon"])}
    else:  # place_name
        kwargs = {"input_place_name": "Maiduguri, Nigeria"}

    mly_downloader.download_svi(output_dir, **kwargs)
    assert len(list(output_dir.iterdir())) >= expected_files


def test_downloader_metadata_only(output, mly_input_files, mly_api_key):
    output_dir = output / "test_metadata"
    if (output_dir / "mly_pids.csv").exists():
        pytest.skip("Result exists")

    mly_downloader = MLYDownloader(
        mly_api_key,
        log_path=str(output_dir / "log.log"),
        max_workers=1,
    )
    mly_downloader.download_svi(output_dir, input_shp_file=mly_input_files["polygon"], metadata_only=True)
    assert (output_dir / "mly_pids.csv").stat().st_size > 0


def test_downloader_with_buffer(output, mly_api_key):
    output_dir = output / "test_buffer"
    mly_downloader = MLYDownloader(mly_api_key, max_workers=1)
    mly_downloader.download_svi(output_dir, lat=11.827575599999989, lon=13.146558000000027, buffer=100)
    assert len(list(output_dir.iterdir())) > 0


def test_downloader_kwargs(output, mly_input_files, mly_api_key):
    output_dir = output / "test_kwargs"
    if (output_dir / "mly_svi").exists():
        pytest.skip("Result exists")

    mly_downloader = MLYDownloader(mly_api_key, log_path=str(output_dir / "log.log"), max_workers=1)
    kwarg = {
        "image_type": "flat",
        "min_captured_at": 1484549945000,
        "max_captured_at": 1642935417694,
        "organization_id": [1805883732926354],
        "compass_angle": (0, 180),
    }
    mly_downloader.download_svi(output_dir, input_shp_file=mly_input_files["polygon"], **kwarg)
    assert len(list(output_dir.iterdir())) > 0


def test_error_handling(output, mly_api_key):
    output_dir = output / "test_errors"
    mly_downloader = MLYDownloader(mly_api_key, log_path=output_dir / "log.log")

    # Test invalid date format
    with pytest.raises(ValueError):
        mly_downloader.download_svi(
            output_dir, lat=11.827575599999989, lon=13.146558000000027, start_date="invalid_date"
        )

    # Test missing required parameters
    with pytest.raises(ValueError):
        mly_downloader.download_svi(output_dir)
