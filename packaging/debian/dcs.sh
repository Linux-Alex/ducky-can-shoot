#!/bin/sh
# Command line entry point of the packaged application.  The package lives in
# /usr/lib/python3/dist-packages, so the distribution interpreter finds it
# without a virtual environment.
exec python3 -m dcs "$@"
