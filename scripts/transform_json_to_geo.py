import argparse
import json
from pathlib import Path


def convert_to_geojson(data):
    features = []

    buffer_geometry = data.get("buffer")
    if buffer_geometry:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "type": "buffer",
                    "display_location": data.get("display_location"),
                },
                "geometry": buffer_geometry,
            }
        )

    field_geojson = data.get("field_delineation", {}).get("fields_geojson", {})
    field_features = field_geojson.get("features", [])
    if field_features:
        features.extend(field_features)

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def default_input_path():
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "/results/result.json"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert a field delineation result JSON file into GeoJSON."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(default_input_path()),
        help="Path to the input result JSON file. Defaults to ./result.json at the repo root.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = input_path.with_suffix(".geojson")

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    geojson_output = convert_to_geojson(data)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(geojson_output, f, indent=2)

    print(f"Wrote GeoJSON to {output_path}")


if __name__ == "__main__":
    main()
