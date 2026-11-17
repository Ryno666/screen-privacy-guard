#!/bin/bash
exec "$(dirname -- "$0")/launch.sh" watch --simulate-self "$@"
