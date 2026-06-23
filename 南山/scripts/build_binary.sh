#!/bin/bash
set -e

pip install pyinstaller flask requests apscheduler -q
rm -rf build dist *.spec
pyinstaller --onefile --name qiangfang app.py
ls -lh dist/
