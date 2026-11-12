from django.db import migrations

PESTS = [
    ("aphids", "Aphids", "insect", 1),
    ("faw", "Fall armyworm", "insect", 2),
    ("mlnd", "Maize lethal necrosis disease (MLND)", "disease", 3),
    ("thrips", "Thrips", "insect", 4),
]

SLD_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>{name}</Name>
    <UserStyle>
      <Title>{title}</Title>
      <FeatureTypeStyle>
        <Rule>
          <RasterSymbolizer>
            <Opacity>1.0</Opacity>
            {colormap}
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""

HYBRID_COLORMAP = """<ColorMap type="ramp">
              <ColorMapEntry color="#1a9641" quantity="0" label="0.0 (low)"/>
              <ColorMapEntry color="#a6d96a" quantity="0.25" label="0.25"/>
              <ColorMapEntry color="#ffffbf" quantity="0.5" label="0.5"/>
              <ColorMapEntry color="#fdae61" quantity="0.75" label="0.75"/>
              <ColorMapEntry color="#d7191c" quantity="1" label="1.0 (high)"/>
            </ColorMap>"""

CLASSIFIED_COLORMAP = """<ColorMap type="values">
              <ColorMapEntry color="#1a9641" quantity="1" label="Very low"/>
              <ColorMapEntry color="#a6d96a" quantity="2" label="Low"/>
              <ColorMapEntry color="#ffffbf" quantity="3" label="Moderate"/>
              <ColorMapEntry color="#fdae61" quantity="4" label="High"/>
              <ColorMapEntry color="#d7191c" quantity="5" label="Very high"/>
            </ColorMap>"""

STYLES = [
    ("pw-hybrid", "Hybrid risk (0–1)", "hybrid", HYBRID_COLORMAP),
    ("pw-classified", "Classified risk (5 classes)", "classified", CLASSIFIED_COLORMAP),
]


def seed(apps, schema_editor):
    Pest = apps.get_model("pestwatch", "Pest")
    PestStyle = apps.get_model("pestwatch", "PestStyle")
    for slug, name, kind, order in PESTS:
        Pest.objects.get_or_create(slug=slug, defaults={"name": name, "kind": kind, "display_order": order})
    # The GeoServer copies are created the first time a layer uses them.
    for name, title, product, colormap in STYLES:
        PestStyle.objects.get_or_create(name=name, defaults={
            "title": title, "product_type": product,
            "sld_body": SLD_HEAD.format(name=name, title=title, colormap=colormap)})


def unseed(apps, schema_editor):
    apps.get_model("pestwatch", "Pest").objects.filter(slug__in=[p[0] for p in PESTS]).delete()
    apps.get_model("pestwatch", "PestStyle").objects.filter(name__in=[s[0] for s in STYLES]).delete()


class Migration(migrations.Migration):
    dependencies = [("pestwatch", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
