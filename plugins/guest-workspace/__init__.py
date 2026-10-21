"""Local plugin entrypoint. This plugin never modifies Hermes source files."""


def register(ctx):
    from tools.guest_workspace_interface import register as register_workspace
    from tools.guest_browser_interface import register as register_browser
    from tools.guest_workspace_maintenance import register as register_maintenance
    register_workspace(ctx)
    register_browser(ctx)
    register_maintenance(ctx)
