#!/usr/bin/env python

from configparser import ConfigParser
from urllib import parse

import click
import maya
import requests
from influxdb import InfluxDBClient
import json



def retrieve_paginated_data(
        api_key, url, from_date, to_date, page=None
):
    args = {
        'period_from': from_date,
        'period_to': to_date,
    }
    if page:
        args['page'] = page
    response = requests.get(url, params=args, auth=(api_key, ''))
    response.raise_for_status()
    data = response.json()
    results = data.get('results', [])
    if data['next']:
        url_query = parse.urlparse(data['next']).query
        next_page = parse.parse_qs(url_query)['page'][0]
        results += retrieve_paginated_data(
            api_key, url, from_date, to_date, next_page
        )
    return results


def store_series(connection, series, c_metrics, e_metrics, rate_data):
    agile_data = rate_data.get('agile_unit_rates', [])
    agile_rates = {
        point['valid_from']: point['value_inc_vat']
        for point in agile_data
    }

    export_data = rate_data.get('export_unit_rates', [])
    export_rates = {
        point['valid_from']: point['value_inc_vat']
        for point in export_data
    }

    def active_rate_field():
        return 'unit_rate_high'

    def fields_for_measurement(measurement, measurement2):
        if iso := measurement.get('interval_start'):
            interval_start = iso
        else:
            interval_start = measurement2.get('interval_start')

        consumption = measurement.get('consumption', 0.0)
        export = measurement2.get('consumption', 0.0)
        rate = active_rate_field()
        rate_cost = rate_data[rate]
        cost = consumption * rate_cost
        standing_charge = rate_data['standing_charge'] / 48  # 30 minute reads
        fields = {
            'consumption': consumption,
            'export': export,
            'cost': cost,
            'total_cost': cost + standing_charge,
        }
        if agile_data:
            agile_standing_charge = rate_data['agile_standing_charge'] / 48
            agile_unit_rate = agile_rates.get(
                maya.parse(interval_start).iso8601(),
                rate_data[rate]  # cludge, use Go rate during DST changeover
            )
            agile_cost = agile_unit_rate * consumption
            fields.update({
                'agile_rate': agile_unit_rate,
                'agile_cost': agile_cost,
                'agile_total_cost': agile_cost + agile_standing_charge,
            })
        if export_data:
            export_unit_rate = export_rates.get(
                maya.parse(interval_start).iso8601(),
                0.0
            )
            export_payment = export_unit_rate * export
            fields.update({
                'export_rate': export_unit_rate,
                'export_payment': export_payment,
            })
            if agile_data:
                fields['agile_total_cost'] -= export_payment

        return fields

    def new_agile_rates(agile_rates, export_rates):
        rates = []
        for date, rate in agile_rates.items():
            fields = {
                'consumption': 0.0,
                'export': 0.0,
                'cost': 0.0,
                'total_cost': 0.0,
            }
            agile_unit_rate = agile_rates.get(
                maya.parse(date).iso8601(),
                0.0
            )
            export_unit_rate = export_rates.get(
                maya.parse(date).iso8601(),
                0.0
            )
            fields.update({
                'agile_rate': agile_unit_rate,
                'agile_cost': 0.0,
                'agile_total_cost': 0.0,
                'export_rate': export_unit_rate,
                'export_payment': 0.0,
            })
            rates.append({ 'date': date, 'fields': fields })
        return rates

    def tags_for_measurement(interval):
        period = maya.parse(interval)
        time = period.datetime().strftime('%H:%M')
        return {
            'active_rate': active_rate_field(),
            'time_of_day': time,
        }

    # Reorganise metrics from lists to arrays keyed by "interval_start"
    c_metrics2 = dict()
    for x in c_metrics:
        c_metrics2[x["interval_start"]] = x

    e_metrics2 = dict()
    for x in e_metrics:
        e_metrics2[x["interval_start"]] = x

    # Combine consumption and export interval keys
    intervals = sorted(list(c_metrics2.keys()) + list(e_metrics2.keys()), reverse=True)

    # Now process by interval keys
    measurements = []
    for interval in intervals:
        c = c_metrics2.get(interval, {})
        e = e_metrics2.get(interval, {})

        measurements.append({
                'measurement': series,
                'tags': tags_for_measurement(interval),
                'time': interval,
                'fields': fields_for_measurement(c, e),
            })

    if agile_data:
        last_usage_time = measurements[0]['time'] if measurements else maya.now().iso8601()
        new_agile = {}
        for k, v in agile_rates.items():
            if k > last_usage_time:
                new_agile[k] = v

        new_export = {}
        for k, v in export_rates.items():
            if k > last_usage_time:
                new_export[k] = v

        new_agile = new_agile_rates(new_agile, new_export)
        if new_agile:
            new_measurements = [
                {
                    'measurement': series,
                    'tags': { 'active_rate': 'unit_rate_high', 'time_of_day': f'{maya.parse(i["date"]).datetime().strftime("%H:%M")}' },
                    'time': i['date'],
                    'fields': i['fields'],
                }
                for i in new_agile
            ]
            measurements.extend(new_measurements)

    # click.echo(json.dumps(measurements, indent=2))
    connection.write_points(measurements)


@click.command()
@click.option(
    '--config-file',
    default="octograph.ini",
    type=click.Path(exists=True, dir_okay=True, readable=True),
)
@click.option('--from-date', default='yesterday midnight', type=click.STRING)
@click.option('--to-date', default='tomorrow 23:59', type=click.STRING)
def cmd(config_file, from_date, to_date):

    config = ConfigParser()
    config.read(config_file)

    influx = InfluxDBClient(
        host=config.get('influxdb', 'host', fallback="localhost"),
        port=config.getint('influxdb', 'port', fallback=8086),
        username=config.get('influxdb', 'user', fallback=""),
        password=config.get('influxdb', 'password', fallback=""),
        database=config.get('influxdb', 'database', fallback="energy"),
    )

    api_key = config.get('octopus', 'api_key')
    if not api_key:
        raise click.ClickException('No Octopus API key set')

    e_mpan = config.get('electricity', 'mpan', fallback=None)
    e_export_mpan = config.get('electricity', 'export_mpan', fallback=None)

    e_serial = config.get('electricity', 'serial_number', fallback=None)
    if not e_mpan or not e_serial:
        raise click.ClickException('No electricity meter identifiers')
    consumption_url = 'https://api.octopus.energy/v1/electricity-meter-points/' \
            f'{e_mpan}/meters/{e_serial}/consumption/'

    export_url = 'https://api.octopus.energy/v1/electricity-meter-points/' \
            f'{e_export_mpan}/meters/{e_serial}/consumption/'

    agile_url = config.get('electricity', 'agile_rate_url', fallback=None)
    agile_export_url = config.get('electricity', 'agile_export_url', fallback=None)

    timezone = config.get('electricity', 'unit_rate_low_zone', fallback='UTC+0')

    rate_data = {
        'electricity': {
            'standing_charge': config.getfloat(
                'electricity', 'standing_charge', fallback=0.0
            ),
            'unit_rate_high': config.getfloat(
                'electricity', 'unit_rate_high', fallback=0.0
            ),
            'unit_rate_low': config.getfloat(
                'electricity', 'unit_rate_low', fallback=0.0
            ),
            'unit_rate_low_start': config.get(
                'electricity', 'unit_rate_low_start', fallback="00:00"
            ),
            'unit_rate_low_end': config.get(
                'electricity', 'unit_rate_low_end', fallback="00:00"
            ),
            'unit_rate_low_zone': timezone,
            'agile_standing_charge': config.getfloat(
                'electricity', 'agile_standing_charge', fallback=0.0
            ),
            'agile_unit_rates': [],
            'export_unit_rates': []
        }
    }

    from_iso = maya.when(from_date, timezone=timezone).iso8601()
    to_iso = maya.when(to_date, timezone=timezone).iso8601()

    click.echo(
        f'Retrieving electricity data for {from_iso} until {to_iso}...',
        nl=False
    )
    e_consumption = retrieve_paginated_data(
        api_key, consumption_url, from_iso, to_iso
    )
    click.echo(f' {len(e_consumption)} readings.')

    click.echo(
        f'Retrieving export data for {from_iso} until {to_iso}...',
        nl=False
    )
    e_export = retrieve_paginated_data(
        api_key, export_url, from_iso, to_iso
    )
    click.echo(f' {len(e_export)} readings.')

    click.echo(
        f'Retrieving Agile rates for {from_iso} until {to_iso}...',
        nl=False
    )
    rate_data['electricity']['agile_unit_rates'] = retrieve_paginated_data(
        api_key, agile_url, from_iso, to_iso
    )
    click.echo(f' {len(rate_data["electricity"]["agile_unit_rates"])} rates.')

    click.echo(
        f'Retrieving Agile export rates for {from_iso} until {to_iso}...',
        nl=False
    )
    rate_data['electricity']['export_unit_rates'] = retrieve_paginated_data(
        api_key, agile_export_url, from_iso, to_iso
    )

    click.echo(f' {len(rate_data["electricity"]["export_unit_rates"])} rates.')

    store_series(influx, 'electricity', e_consumption, e_export, rate_data['electricity'])

if __name__ == '__main__':
    cmd()