#!/bin/sh
# tools/docs.sh <folder>: the API reference (pdoc) of the package and every public module, into <folder>. The package
# lists its names in __all__, so pdoc documents a submodule only when it is named here. Docstrings are Google style.
set -eu
pdoc --docformat google --output-directory "$1" \
  pdigger_factory_sdk \
  pdigger_factory_sdk.api \
  pdigger_factory_sdk.client \
  pdigger_factory_sdk.codec \
  pdigger_factory_sdk.errors \
  pdigger_factory_sdk.interpreter \
  pdigger_factory_sdk.models \
  pdigger_factory_sdk.replay \
  pdigger_factory_sdk.session \
  pdigger_factory_sdk.streams \
  pdigger_factory_sdk.testing \
  pdigger_factory_sdk.text
