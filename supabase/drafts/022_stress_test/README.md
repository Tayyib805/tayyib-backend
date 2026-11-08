# Halal Maps schema stress test

Runs the draft schema (`../022_halal_maps.sql`) on a plain local Postgres 16,
with a small stand-in for PostGIS distance functions.

```sh
createdb maps_test
psql -d maps_test -f 00_postgis_shim.sql -f 01_extensions.sql
sed -e 's/^create extension if not exists .*//' -e 's/geography(point, 4326)/geopt/' \
    -e 's/::geography//g' -e '/places_location_idx/d' ../022_halal_maps.sql | psql -d maps_test -q
psql -d maps_test -f 10_scenarios.sql   # 14 failure scenarios; one intended ERROR (S10: bad certifier refused)
psql -d maps_test -f 20_load.sql        # 20,000 places: timings and storage
```

Results (Nov 8, 2026) are in the design doc's "Stress test" section.
