#!/bin/bash
exec /usr/bin/cc "$@" -L$PROJECT_ROOT/workspaces/montage/20260706_062459/venv/lib/python3.13/site-packages/dftracer/lib64 -Wl,-rpath,$PROJECT_ROOT/workspaces/montage/20260706_062459/venv/lib/python3.13/site-packages/dftracer/lib64 -ldftracer_core
