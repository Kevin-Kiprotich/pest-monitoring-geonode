"""Import administrative boundaries from GeoNode vector datasets.

Wards and constituencies are uploaded through GeoNode's normal uploader,
which stores them in the PostGIS datastore. Here their features are copied
into AdminBoundary so they can be used for county checks and zonal statistics.
"""
import re
from collections import defaultdict

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.db import connections, transaction
from psycopg2 import sql

from .models import PARENT_LEVEL, AdminBoundary, AdminLevel

DATASTORE = "datastore"


class BoundaryImportError(ValueError):
    pass


def _name_key(name):
    """Normalise a boundary name for matching: "Mumias West  Sub County" -> "mumias west"."""
    key = " ".join(name.lower().split())
    return re.sub(r"\s+(sub[\s-]?county|constituency|county)$", "", key)


def _geometry_column(table):
    with connections[DATASTORE].cursor() as c:
        c.execute(
            "SELECT f_table_schema, f_geometry_column, type FROM geometry_columns WHERE f_table_name = %s",
            [table])
        row = c.fetchone()
    if row is None:
        raise BoundaryImportError(
            f"{table} is not stored in GeoNode's PostGIS datastore, so it can't be imported.")
    schema, column, geom_type = row
    if geom_type.upper() not in {"POLYGON", "MULTIPOLYGON", "GEOMETRY"}:
        raise BoundaryImportError(f"{table} has {geom_type.lower()} geometries; boundaries must be polygons.")
    return schema, column


def dataset_fields(dataset):
    """Non-geometry column names of a dataset's datastore table."""
    schema, geom_col = _geometry_column(dataset.name)
    with connections[DATASTORE].cursor() as c:
        c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            [schema, dataset.name])
        return [r[0] for r in c.fetchall() if r[0] != geom_col]


def read_features(dataset, name_field, code_field="", parent_field=""):
    """Yield (name, code, parent_name, GEOSGeometry in 4326) for each feature."""
    schema, geom_col = _geometry_column(dataset.name)
    fields = dataset_fields(dataset)
    for f in filter(None, (name_field, code_field, parent_field)):
        if f not in fields:
            raise BoundaryImportError(f"{dataset.name} has no attribute called {f}.")

    def col(name):
        return sql.Identifier(name) if name else sql.SQL("NULL")

    query = sql.SQL(
        "SELECT {name}::text, {code}::text, {parent}::text, "
        "ST_AsBinary(ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform({geom}, 4326)), 3))) "
        "FROM {table} WHERE {geom} IS NOT NULL"
    ).format(
        name=col(name_field), code=col(code_field), parent=col(parent_field),
        geom=sql.Identifier(geom_col), table=sql.Identifier(schema, dataset.name))

    with connections[DATASTORE].cursor() as c:
        c.execute(query.as_string(c.connection))
        for name, code, parent, wkb in c.fetchall():
            if not name or not wkb:
                continue
            # GEOS reads bytes as WKT/hex; WKB must be passed as a memoryview.
            yield name.strip(), (code or "").strip(), (parent or "").strip(), GEOSGeometry(memoryview(wkb), srid=4326)


def _as_multipolygon(geom):
    if isinstance(geom, MultiPolygon):
        return geom
    if geom.geom_type == "Polygon":
        return MultiPolygon(geom, srid=geom.srid)
    polys = [g for g in geom if g.geom_type == "Polygon"] if hasattr(geom, "__iter__") else []
    return MultiPolygon(*polys, srid=geom.srid)


def _union(geoms):
    out = geoms[0]
    for g in geoms[1:]:
        out = out.union(g)
    return _as_multipolygon(out)


def _spatial_parent(geom, candidates):
    point = geom.point_on_surface
    for cand in candidates:
        if cand.geom.contains(point):
            return cand
    return None


@transaction.atomic
def import_boundaries(source, dataset):
    """Import ``dataset`` as ``source.level`` boundaries. Returns the affected counties."""
    level = source.level
    limit = source.limit_set()

    # Group parts that share a name (and parent) into one boundary.
    grouped = defaultdict(lambda: {"code": "", "geoms": []})
    for name, code, parent, geom in read_features(dataset, source.name_field, source.code_field, source.parent_field):
        if level == AdminLevel.COUNTY:
            if limit and name.lower() not in limit:
                continue
            parent = ""
        entry = grouped[(name, parent)]
        entry["code"] = entry["code"] or code
        entry["geoms"].append(geom)
    if not grouped:
        raise BoundaryImportError("No features were found to import. Check the attribute names and the limit list.")

    parent_level = PARENT_LEVEL.get(level)
    parents = list(AdminBoundary.objects.filter(level=parent_level)) if parent_level else []
    parents_by_name = defaultdict(list)
    for p in parents:
        parents_by_name[_name_key(p.name)].append(p)

    derived_counties = {}
    seen, unmatched = set(), 0
    for (name, parent_name), entry in grouped.items():
        geom = _union(entry["geoms"])
        parent = None
        if parent_level:
            if limit and parent_name and parent_name.lower() not in limit:
                continue
            if parent_name:
                matches = parents_by_name.get(_name_key(parent_name), [])
                parent = matches[0] if len(matches) == 1 else _spatial_parent(geom, matches) if matches else None
                if parent is None:
                    # Names often differ between datasets ("Malava Sub County" vs "Malava"):
                    # fall back to the parent that contains the boundary.
                    parent = _spatial_parent(geom, parents)
                if parent is None and level == AdminLevel.CONSTITUENCY:
                    # No county layer: derive the county from the constituencies.
                    parent = derived_counties.get(_name_key(parent_name))
                    if parent is None:
                        parent, _ = AdminBoundary.objects.get_or_create(
                            level=AdminLevel.COUNTY, name=parent_name, parent=None,
                            defaults={"source": source, "geom": geom})
                        derived_counties[_name_key(parent_name)] = parent
                        parents_by_name[_name_key(parent_name)].append(parent)
            else:
                parent = _spatial_parent(geom, parents)
            # Boundaries outside every known parent (e.g. wards of other counties
            # in a national dataset) are of no use here: count and skip them.
            if parent is None:
                unmatched += 1
                continue
            if limit and parent.name.lower() not in limit:
                continue

        obj, _ = AdminBoundary.objects.update_or_create(
            level=level, name=name, parent=parent,
            defaults={"code": entry["code"], "geom": geom, "source": source})
        seen.add(obj.pk)

    # Derived counties take the shape of their constituencies.
    for county in derived_counties.values():
        children = list(county.children.filter(level=AdminLevel.CONSTITUENCY).values_list("geom", flat=True))
        if children:
            county.geom = _union(children)
            county.save(update_fields=["geom", "updated_at"])

    # Remove boundaries this source no longer contains, unless a layer depends on them.
    stale = AdminBoundary.objects.filter(source=source, level=level).exclude(pk__in=seen)
    stale.filter(pest_layers__isnull=True).delete()

    source.feature_count = len(seen)
    source.unmatched_count = unmatched
    source.save(update_fields=["feature_count", "unmatched_count", "updated_at"])

    counties = {b.county for b in AdminBoundary.objects.filter(pk__in=seen)}
    counties.update(derived_counties.values())
    counties.discard(None)
    return counties
