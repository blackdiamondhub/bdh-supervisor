from bdh_sdk.plugins import PluginSpec, Tile

spec = PluginSpec(
    id="hello",
    app="bdh_hello.apps.HelloConfig",
    urls="bdh_hello.urls",
    tiles=(Tile(title="Hello", url_name="hello_index", icon="bdh_hello/icon.svg", section="apps"),),
    network="none",
    lifecycle="on_demand",
    memory_mb=96,
)
