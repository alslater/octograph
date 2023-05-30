cd ~/src/octograph

. .venv/bin/activate

date >> ~/logs/octograph.log
python app/octopus_to_influxdb.py >> ~/logs/octograph.log 2>&1
