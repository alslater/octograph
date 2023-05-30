cd /home/aslate/src/octograph

. .venv/bin/activate

date >> /home/aslate/logs/octograph.log
python app/octopus_to_influxdb.py >> /home/aslate/logs/octograph.log 2>&1
