import pytest
from pathlib import Path
from zensvi.cv import ClassifierWeather


@pytest.fixture
def output(base_output_dir, ensure_dir):
    output_dir = base_output_dir / "classification/weather"
    ensure_dir(output_dir)
    return output_dir


def test_classify_directory(output, input_dir, all_devices):
    classifier = ClassifierWeather(device=all_devices)
    image_input = str(input_dir / "images")
    dir_summary_output = str(output / f"{all_devices}/directory/summary")
    classifier.classify(
        image_input,
        dir_summary_output=dir_summary_output,
        batch_size=3,
    )
    assert len(list(Path(dir_summary_output).iterdir())) > 0


def test_classify_single_image(output, input_dir, all_devices):
    classifier = ClassifierWeather(device=all_devices)
    image_input = str(input_dir / "images/-3vfS0_iiYVZKh_LEVlHew.jpg")
    dir_summary_output = str(output / f"{all_devices}/single/summary")
    classifier.classify(
        image_input,
        dir_summary_output=dir_summary_output,
    )
    assert len(list(Path(dir_summary_output).iterdir())) > 0
