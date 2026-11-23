"""Combine all or selected polygon features into an area of interest."""

import shapely


def union_polygon_features(features, selected_ids=None):
    """Return the polygon union, optionally restricted to selected feature IDs."""
    selected_ids = set(selected_ids) if selected_ids is not None else None
    polygons = [
        geometry
        for feature_id, geometry in features
        if selected_ids is None or feature_id in selected_ids
    ]
    if not polygons:
        if selected_ids is not None:
            raise ValueError("The selected polygon features contain no usable area.")
        raise ValueError("The AOI layer contains no polygon geometry.")

    geometry = shapely.union_all(polygons)
    if geometry.is_empty:
        raise ValueError("The AOI geometry is empty.")
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    if geometry.geom_type == "GeometryCollection":
        geometry = shapely.union_all(
            [part for part in geometry.geoms if part.geom_type in ("Polygon", "MultiPolygon")]
        )
    if geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("The AOI must contain polygon geometry.")
    return geometry


def gauges_in_geometry(geometry, gauges):
    """Keep only rain gauges inside an explicitly selected AOI."""
    return [
        gauge for gauge in gauges
        if geometry.covers(shapely.Point(float(gauge["lon"]), float(gauge["lat"])))
    ]
