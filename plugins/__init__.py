from importlib import import_module


def load(name):
    return import_module(f"plugins.{name}")
