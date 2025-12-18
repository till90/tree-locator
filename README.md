# Tree Locator (Cloud Run)

Kleiner Flask-Dienst, der einen Ort per Nominatim auflöst und über die Overpass API die Anzahl von `natural=tree` (OpenStreetMap) liefert. Optional gibt es ein Sample für eine Mini-Map sowie GeoJSON-Export.

## Lokales Starten

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
