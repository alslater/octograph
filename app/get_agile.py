#!/usr/bin/env python

from configparser import ConfigParser
from urllib import parse

import click
import maya
import requests
import calendar
import hashlib
import os
from influxdb import InfluxDBClient
from datetime import datetime, timedelta

from alive_progress import alive_bar

from json import dumps
import json
from sys import exit
import numpy as np
from scipy.stats import norm


# See https://www.timeanddate.com/sun/@2647655?month=12&year=2024 went with the 15th as average for the month
hours_of_daylight = {
    1: list(range(9, 15)),
    2: list(range(8, 16)),
    3: list(range(7, 17)),
    4: list(range(7, 19)),
    5: list(range(6, 20)),
    6: list(range(6, 20)),
    7: list(range(6, 20)),
    8: list(range(7, 19)),
    9: list(range(8, 18)),
    10: list(range(8, 17)),
    11: list(range(8, 15)),
    12: list(range(9, 15)),
}

# Pulled this from the code at https://www.in2gr8tedsolutions.co.uk/info/solar-generation-calculator.php?#
# def calculate_monthly_solar(kwh):
    # return [
    #     round(kwh * 0.03),
    #     round(kwh * 0.045),
    #     round(kwh * 0.088),
    #     round(kwh * 0.11),
    #     round(kwh * 0.12),
    #     round(kwh * 0.135),
    #     round(kwh * 0.14),
    #     round(kwh * 0.115),
    #     round(kwh * 0.09),
    #     round(kwh * 0.06),
    #     round(kwh * 0.042),
    #     round(kwh * 0.025)
    # ]

# Slightly more accurate monthly hours of sunlight from https://www.metoffice.gov.uk/research/climate/maps-and-data/uk-climate-averages/u101x20r9
# From Herstmonceux weather station a few miles up the road
def calculate_monthly_solar(kwh):
    return [
        round(kwh * 0.034, 4),
        round(kwh * 0.046, 4),
        round(kwh * 0.075, 4),
        round(kwh * 0.108, 4),
        round(kwh * 0.128, 4),
        round(kwh * 0.130, 4),
        round(kwh * 0.134, 4),
        round(kwh * 0.120, 4),
        round(kwh * 0.091, 4),
        round(kwh * 0.066, 4),
        round(kwh * 0.038, 4),
        round(kwh * 0.03, 4)
    ]

battery_charge_start = 2
battery_charge_end = 5
battery_charge_end_minutes = 5 * 60 + 30
winter_months = [1,2,11,12]
winter_battery_start = 15
winter_battery_start_mins = 30

summer_months = []
summer_charge_battery = False
summer_charge_when_negative = False

def calculate_daily_curve(total, hours):
    # hours is a list of hours of daylight, ie 9,10,11,12,13,14
    # We need a curve for 30 minute "slots"
    slots = list(range(len(hours) * 2))

    # Calculate the mean and standard deviation
    mean = np.mean(slots)
    std_dev = np.std(slots)

    # Generate the bell curve using the normal distribution
    y_values = norm.pdf(slots, mean, std_dev)

    # Normalize the values to sum up to the total
    y_values /= np.sum(y_values)
    y_values *= total

    return y_values.tolist()


curves = {}

def calculate_30min_solar(monthly_solar, year, month, hour, minute):
    days_in_month = calendar.monthrange(year, month)[1]
    hours = hours_of_daylight[month]
    if hour not in hours:
        return 0

    # Just calculate the curve once per month
    if curves.get(month) is None:
        # Calculate the amount of generation in a day
        daily_solar = monthly_solar[month - 1] / days_in_month

        # Now fit it to a normal distribution
        daily_curve = calculate_daily_curve(daily_solar, hours)
        curves[month] = daily_curve

    curve = curves.get(month)
    # index is the hour and minute converted to an index from 0
    index = (hour - hours[0]) * 2 + (minute // 30)
    return curve[index]


    #return daily_solar / len(hours) / 2 if hour in hours else 0


def are_we_using_the_battery(month, hour, mins):
    time_minutes = (hour * 60) + mins
    wbs_mins = (winter_battery_start * 60) + winter_battery_start_mins
    if month in winter_months:
        return (hour < battery_charge_start) or time_minutes >= wbs_mins
    if month in summer_months and summer_charge_battery == False:
        return True
    # So, we must be in spring/autumn then
    return hour < battery_charge_start or time_minutes >= battery_charge_end_minutes


def retrieve_paginated_data(api_key, url, from_date, to_date, page=None, bar=None):
    # deepcode ignore InsecureHash: just calculating a filename
    filename = 'cache/' + hashlib.md5((url+from_date+to_date).encode('utf-8')).hexdigest() + '.json'
    if os.path.exists(filename):
        with open(filename, 'r') as file:
            if bar is not None:
                bar()
            return json.load(file)

    args = {
        'period_from': from_date,
        'period_to': to_date,
    }
    if page:
        args['page'] = page

    attempt = 1
    while True:
        response = requests.get(url, params=args, auth=(api_key, ''))
        if response.status_code == 200:
            break
        attempt += 1
        if attempt > 5:
            response.raise_for_status()

    if bar is not None:
        bar()

    data = response.json()
    results = data.get('results', [])
    if data['next']:
        url_query = parse.urlparse(data['next']).query
        next_page = parse.parse_qs(url_query)['page'][0]
        results += retrieve_paginated_data(api_key, url, from_date, to_date, next_page, bar)

    if page is None:
        with open(filename, 'w') as file:
            json.dump(results, file)

    return results


def store_series(connection, series, metrics):

    def active_rate_field(measurement):
        return 'unit_rate_high'

    def fields_for_measurement(measurement):
        consumption = measurement['consumption']
        rate = active_rate_field(measurement)
        fields = {
            'consumption': float(consumption),
            'agile_rate': float(measurement['agile_rate']),
            'agile_export_rate': float(measurement['agile_export_rate']),
            'agile_cost': float(measurement['agile_cost']),
            'flux_rate': float(measurement['flux_rate']),
            'flux_cost': float(consumption * measurement['flux_rate']),
            'total_cost': float(measurement['agile_cost']) + 41.03 / 48,
            'battery_charge': float(measurement['current_battery']),
            'solar_generation': float(measurement['solar_generation']),
            'solar_export': float(measurement['solar_export']),
            'actual_usage': float(measurement['usage']),
        }
        return fields

    def tags_for_measurement(date, measurement):
        period = maya.parse(date)
        time = period.datetime().strftime('%H:%M')
        return {
            'active_rate': active_rate_field(measurement),
            'time_of_day': time,
        }

    measurements = []

    for date, data in metrics.items():
        measurements.append(
            {
                'measurement': series,
                'tags': tags_for_measurement(date, data),
                'time': date,
                'fields': fields_for_measurement(data),
            }
        )
    import json

    # print(json.dumps(measurements, indent=2))

    connection.write_points(measurements)


@click.command()
@click.option(
    '--config-file',
    default="octograph.ini",
    type=click.Path(exists=True, dir_okay=True, readable=True),
)
@click.option('--from-date', default='yesterday midnight', type=click.STRING)
@click.option('--to-date', default='tomorrow midnight', type=click.STRING)
@click.option('--write-db', is_flag=True)
def cmd(config_file, from_date, to_date, write_db):

    config = ConfigParser()
    config.read(config_file)

    if write_db:
        influx = InfluxDBClient(
            host=config.get('influxdb', 'host', fallback="localhost"),
            port=config.getint('influxdb', 'port', fallback=8086),
            username=config.get('influxdb', 'user', fallback=""),
            password=config.get('influxdb', 'password', fallback=""),
            database='solar'
        )

    api_key = config.get('octopus', 'api_key')
    if not api_key:
        raise click.ClickException('No Octopus API key set')

    e_mpan = config.get('electricity', 'mpan', fallback=None)
    e_serial = config.get('electricity', 'serial_number', fallback=None)
    if not e_mpan or not e_serial:
        raise click.ClickException('No electricity meter identifiers')
    e_url = 'https://api.octopus.energy/v1/electricity-meter-points/' \
            f'{e_mpan}/meters/{e_serial}/consumption/'
    agile_url = config.get('electricity', 'agile_rate_url', fallback=None)

    agile_url2 = config.get('electricity', 'agile_rate2_url', fallback=None)
    agile_rete2_date = config.get('electricity', 'agile_rate2_date', fallback=None)
    agile_export_url = config.get('electricity', 'agile_export_url', fallback=None)

    timezone = config.get('electricity', 'battery_zone', fallback='Europe/London')

    flux_rate_low = config.getfloat('electricity', 'flux_rate_low', fallback=16.86)
    flux_rate_day = config.getfloat('electricity', 'flux_rate_day', fallback=28.10)
    flux_rate_peak = config.getfloat('electricity', 'flux_rate_peak', fallback=39.34)

    global battery_charge_start, battery_charge_end, winter_months, summer_months, summer_charge_battery, summer_charge_when_negative, winter_battery_start, winter_battery_start_mins, battery_charge_end_minutes

    battery_charge_start = config.getfloat('electricity', 'battery_charge_start', fallback=0.0)
    battery_charge_start_minute = config.getfloat('electricity', 'battery_charge_start_minute', fallback=0.0)
    battery_charge_end = config.getfloat('electricity', 'battery_charge_end', fallback=0.0)
    battery_charge_end_minute = config.getfloat('electricity', 'battery_charge_end_minute', fallback=0.0)

    battery_charge_start_minutes = battery_charge_start * 60 + battery_charge_start_minute
    battery_charge_end_minutes = battery_charge_end * 60 + battery_charge_end_minute

    battery_max = config.getfloat('electricity', 'battery_max', fallback=0.0)
    inverter_limit = config.getfloat('electricity', 'inverter_limit', fallback=0.0)
    battery_min = config.getfloat('electricity', 'battery_min', fallback=0.0)
    annual_output = config.getfloat('solar', 'annual_output', fallback=0.0)
    monthly_solar = calculate_monthly_solar(annual_output)
    winter_months = config.get('electricity', 'winter_months', fallback='1,2,11,12').split(',')
    winter_months = [int(month) for month in winter_months]
    winter_battery_start = config.getfloat('electricity', 'winter_battery_start', fallback=15)
    winter_battery_start_mins = config.getfloat('electricity', 'winter_battery_start_mins', fallback=0)

    summer_months = config.get('electricity', 'summer_months', fallback='5,6,7,8').split(',')
    summer_months = [int(month) for month in summer_months]

    summer_charge_battery = config.getboolean('electricity', 'summer_charge_battery', fallback=True)
    summer_charge_when_negative = config.getboolean('electricity', 'summer_charge_when_negative', fallback=False)

    model_cutoff = maya.when(config.get('solar', 'model_cutoff', fallback='18th Feb 2024'))

    click.echo('Parameters')
    click.echo(f'Battery max             : {battery_max}')
    click.echo(f'Battery max charge rate : {inverter_limit}')
    click.echo(f'Battery charge from     : {int(battery_charge_start)}:{int(battery_charge_start_minute)} to {int(battery_charge_end)}:{int(battery_charge_end_minute)}')
    click.echo(f'Winter months           : {winter_months}')
    click.echo(f'Summer months           : {summer_months}')
    click.echo(f'Summer charge battery   : {summer_charge_battery}')
    click.echo(f'Summer charge negative  : {summer_charge_when_negative}')
    click.echo(f'Monthly solar           : {monthly_solar}')
    click.echo(f'Annual solar            : {sum(monthly_solar)}')
    click.echo()

    from_maya = maya.when(from_date, timezone=timezone)
    to_maya = maya.when(to_date, timezone=timezone)
    switch_maya = maya.when(agile_rete2_date, timezone=timezone)

    from_iso = maya.when(from_date, timezone=timezone).iso8601()
    to_iso = maya.when(to_date, timezone=timezone).iso8601()

    click.echo(f'Processing for {from_iso} until {to_iso}')

    rate_data = []

    # If the to date is after the tariff switch date, we need to get the new tariff data
    # from the switch date or from_date, whichever is later
    if to_maya >= switch_maya:
        title = f'Retrieving agile rate data rate 2'
        with alive_bar(0, title=title, theme='classic') as bar:
            if from_maya <= switch_maya:
                fromiso = switch_maya.iso8601()
            else:
                fromiso = from_iso
            rate_data.extend(retrieve_paginated_data(api_key, agile_url, fromiso, to_iso, None, bar))

    # If the from date is before the tariff switch date, we need to get the old tariff data
    # to the switch date or to_date, whichever is earlier
    if from_maya < switch_maya:
        title = f'Retrieving agile rate data rate 1'
        with alive_bar(0, title=title, theme='classic') as bar:
            if to_maya >= switch_maya:
                toiso = switch_maya.iso8601()
            else:
                toiso = to_iso
            rate_data.extend(retrieve_paginated_data(api_key, agile_url2, from_iso, toiso, None, bar))

    title = f'Retrieving agile export rate data'
    with alive_bar(0, title=title, theme='classic') as bar:
        export_rate_data = retrieve_paginated_data(api_key, agile_export_url, from_iso, to_iso, None, bar)

    # print(dumps(rate_data, indent=2))

    flux_period_averages = {}
    flux_total = 0
    agile_daily_averages = {}
    agile_daily_total = 0
    agile_peak_averages = {}
    agile_peak_total = 0

    agile_rates = {}
    agile_export_rates = {}

    current_battery = 0

    last_date = ''

    title='Processing import rates          '
    with alive_bar(len(rate_data), title=title, theme='classic') as bar:
        rate_data = reversed(rate_data)
        for rate in rate_data:
            # print(dumps(rate, indent=2))
            dt = maya.parse(rate['valid_from']).datetime(to_timezone='Europe/London')
            dtiso = maya.parse(rate['valid_from']).iso8601()

            # Extract date and hour components
            date = dt.date().strftime("%Y-%m-%d")
            hour = dt.hour
            minute = dt.minute

            if date != last_date:
                last_date = date
                flux_period_averages[date] = 0
                agile_daily_averages[date] = 0
                agile_peak_averages[date] = 0
                agile_rates[dtiso] = {}

            agile_rates[dtiso] = rate['value_inc_vat']

            # Day rate between midnight and 2am, 4 30min periods
            if hour >= 0 and hour < 2:
                agile_daily_averages[date] += rate['value_inc_vat']

            # Flux period rate between 2am and 5am
            if hour >= 2 and hour < 5:
                flux_period_averages[date] += rate['value_inc_vat']

            if hour == 5 and minute == 30:
                flux_total += flux_period_averages[date]
                flux_period_averages[date] /= 6

            # Day rate between 5am and 4pm, 22 30 min periods
            if hour >= 5 and hour < 16:
                agile_daily_averages[date] += rate['value_inc_vat']

            # Peak rate between 4pm and 7pm
            if hour >= 16 and hour < 19:
                agile_peak_averages[date] += rate['value_inc_vat']

            if hour == 18 and minute == 30:
                agile_peak_total += agile_peak_averages[date]
                agile_peak_averages[date] /= 6

            # Day rate between 7pm and midnight, 8 30min periods
            if hour >= 19 and hour <= 23:
                agile_daily_averages[date] += rate['value_inc_vat']

            if hour == 23 and minute == 30:
                agile_daily_total += agile_daily_averages[date]
                agile_daily_averages[date] /= 34

            bar()

    title='Processing export rates          '
    with alive_bar(len(export_rate_data), title=title, theme='classic') as bar:
        for rate in reversed(export_rate_data):
            dt = maya.parse(rate['valid_from']).datetime(to_timezone='Europe/London')
            dtiso = maya.parse(rate['valid_from']).iso8601()

            # Extract date and hour components
            date = dt.date().strftime("%Y-%m-%d")
            hour = dt.hour
            minute = dt.minute

            agile_export_rates[dtiso] = rate['value_inc_vat']
            bar()


    if not flux_period_averages:
        click.echo('\nNo agile data found')
        exit(1)

    # click.echo(dumps(agile_rates, indent=2))

    title = 'Retrieving consumption data      '
    with alive_bar(0, title=title, theme='classic') as bar:
        usage_data = retrieve_paginated_data(api_key, e_url, from_iso, to_iso, None, bar)

    last_date = ''

    costs = {}

    total_consumption = 0
    grid_consumption = 0

    grid_usage = {}

    required_charge = 0.0
    charging = False

    current_day = 0
    daily_grid_consumption = 0

    days_of_zero_grid = 0

    title='Processing consumption data      '
    with alive_bar(len(usage_data), title=title, theme='classic') as bar:
        max_battery_drain = 6.6 / 2 # inverter_limit / 2
        for usage_period in reversed(usage_data):
            dt_maya = maya.parse(usage_period['interval_start'])
            dt = dt_maya.datetime(to_timezone='Europe/London')
            dtiso = maya.when(usage_period['interval_start']).iso8601()
            # Extract date and hour components
            date = dt.date().strftime("%Y-%m-%d")
            hour = dt.hour
            month = dt.month
            minute = dt.minute
            year = dt.year
            day = dt.day

            if day != current_day:
                current_day = day
                if daily_grid_consumption == 0:
                    days_of_zero_grid += 1
                daily_grid_consumption = 0

            # print(f'{dtiso=} {hour=} {current_battery=} consumption={usage_period["consumption"]}')

            if date != last_date:
                last_date = date
                costs[date] = {
                    'agile_rate' : 0,
                    'agile_export_rate' : 0,
                    'agile_cost' : 0,
                    'flux_cost' : 0,
                    'consumption': 0,
                    'export': 0,
                    'previous_cost': 0,
                    'flux_total': 0,
                    'solar_generated': 0
                }

            solar_generated = calculate_30min_solar(monthly_solar, year, month, hour, minute)
            costs[date]['solar_generated'] += solar_generated

            # Set the flux rate
            flux_rate = flux_rate_day

            if hour >= 2 and hour < 5:
                flux_rate = flux_rate_low
            elif hour >= 16 and hour < 19:
                flux_rate = flux_rate_peak


            grid_usage[dtiso] = {
                'consumption': 0.0,
                'agile_rate': agile_rates[dtiso],
                'agile_export_rate': agile_export_rates[dtiso],
                'agile_cost': 0,
                'flux_rate': flux_rate,
                'current_battery': current_battery,
                'solar_generation': solar_generated,
                'solar_export': 0.0,
                'usage': usage_period['consumption']
            }

            total_consumption += usage_period['consumption']
            costs[date]['consumption'] += usage_period['consumption']
            costs[date]['previous_cost'] += usage_period['consumption'] * agile_rates[dtiso]
            costs[date]['flux_total'] += usage_period['consumption'] * flux_rate

            current_minutes = hour * 60 + minute

            # Charge the battery from the mains between the configured hours.
            # I am assuming that we don't use the battery during charging...
            need_to_charge_battery = (
                    battery_max > 0 and
                    (
                        current_minutes >= battery_charge_start_minutes and
                        current_minutes < battery_charge_end_minutes and
                        current_battery < battery_max
                    ) and (
                        summer_charge_battery or month not in summer_months
                    )
                ) or (
                    summer_charge_when_negative and month in summer_months and agile_rates[dtiso] < 0
                )

            if need_to_charge_battery:
                if not charging:
                    # Start the charging period
                    charging = True

                required_charge = battery_max - current_battery

                if dt_maya < model_cutoff:
                    # We can charge at most battery_max_charge per hour
                    period_charge = min(required_charge, inverter_limit/2)
                else:
                    period_charge = 0

                grid_usage[dtiso]['consumption'] = period_charge + usage_period['consumption']
                grid_consumption += period_charge + usage_period['consumption']
                daily_grid_consumption += period_charge + usage_period['consumption']

                costs[date]['flux_cost'] += (period_charge + usage_period['consumption']) * flux_rate_low
                costs[date]['agile_cost'] += (period_charge + usage_period['consumption']) * agile_rates[dtiso]
                grid_usage[dtiso]['agile_cost'] += (period_charge + usage_period['consumption']) * agile_rates[dtiso]

                if dt_maya < model_cutoff:
                    current_battery += period_charge
                else:
                    current_battery += min(required_charge, inverter_limit/2)

            else:
                usage = usage_period['consumption']

                # Handle the solar generation
                if solar_generated >= usage:
                    # Ok, so we generated more solar than we used in this period
                    # So, consumption is zero
                    grid_usage[dtiso]['consumption'] = 0

                    # We have some to use to charge the battery or export
                    excess_power = solar_generated - usage

                    # Export whatever excess power we have over the battery capacity
                    if current_battery + excess_power > battery_max:
                        export = current_battery + excess_power - battery_max
                        grid_usage[dtiso]['solar_export'] = export
                        costs[date]['export'] += export
                        costs[date]['agile_cost'] -= export * agile_export_rates[dtiso]
                        grid_usage[dtiso]['agile_cost'] -= export * agile_export_rates[dtiso]

                    # Charge the battery with the excess power up to capacity
                    current_battery = min(current_battery + excess_power, battery_max)

                    usage = 0
                else:
                    usage = usage - solar_generated

                # If there is any power left to account for after solar generation has been accounted for
                if usage > 0:
                    # Battery usage is limited to certain hours
                    using_battery = battery_max > 0 and are_we_using_the_battery(month, hour, minute)

                    if using_battery:
                        if (current_battery - battery_min) > usage:
                            # We are using the battery and there is enough charge for this period
                            # Need to ensure we are capping battery usage at the inverter power limit
                            if usage > max_battery_drain:
                                current_battery -= max_battery_drain
                                usage -= max_battery_drain
                            else:
                                grid_usage[dtiso]['consumption'] = 0
                                current_battery -= usage
                                usage = 0
                        else:
                            # Use up remaining battery charge
                            if (current_battery - battery_min) > max_battery_drain:
                                usage = usage - max_battery_drain
                                current_battery -= max_battery_drain
                            else:
                                usage = usage - (current_battery - battery_min)
                                current_battery = battery_min

                    if usage > 0:
                        grid_consumption += usage
                        daily_grid_consumption += usage
                        costs[date]['flux_cost'] += usage * flux_rate
                        costs[date]['agile_cost'] += usage * agile_rates[dtiso]
                        grid_usage[dtiso ]['agile_cost'] += usage * agile_rates[dtiso]

                        grid_usage[dtiso]['consumption'] = usage

            bar()


    click.echo()
    click.echo(f'low period\tagile average = {round(flux_total/len(flux_period_averages)/6, 2)}\tflux = {flux_rate_low}')
    click.echo(f'day period\tagile average = {round(agile_daily_total/len(agile_daily_averages)/34, 2)}\tflux = {flux_rate_day}')
    click.echo(f'peak period\tagile average = {round(agile_peak_total/len(agile_peak_averages)/6, 2)}\tflux = {flux_rate_peak}')
    click.echo(f'agile low/peak difference\t = {round((agile_peak_total/len(agile_peak_averages)/6) - (flux_total/len(flux_period_averages)/6), 2)}')
    click.echo(f'flux low/peak difference\t = {round(flux_rate_peak - flux_rate_low, 2)}')
    click.echo()

    last_month = ''
    monthly_totals = {}
    totals = {'agile_cost':0, 'flux_cost':0, 'export': 0, 'previous_cost': 0, 'flux_total': 0, 'solar_generated': 0}

    for date, data in costs.items():
        month = date[:7]
        if month != last_month:
            monthly_totals[month] = {'agile_cost':0, 'flux_cost':0, 'consumption': 0, 'export': 0, 'previous_cost': 0, 'flux_total': 0, 'solar_generated': 0}
            last_month = month

        monthly_totals[month]['agile_cost'] += data['agile_cost']
        monthly_totals[month]['flux_cost'] += data['flux_cost']
        monthly_totals[month]['consumption'] += data['consumption']
        monthly_totals[month]['export'] += data['export']
        monthly_totals[month]['previous_cost'] += data['previous_cost']
        monthly_totals[month]['flux_total'] += data['flux_total']
        monthly_totals[month]['solar_generated'] += data['solar_generated']

        totals['agile_cost'] += data['agile_cost']
        totals['flux_cost'] += data['flux_cost']
        totals['export'] += data['export']
        totals['previous_cost'] += data['previous_cost']
        totals['flux_total'] += data['flux_total']
        totals['solar_generated'] += data['solar_generated']

    click.echo()
    for date, data in monthly_totals.items():
        click.echo(f'{date} : agile = {round(data["agile_cost"]/100,2 )} flux = {round(data["flux_cost"]/100, 2)} consumption = {round(data["consumption"], 2)}kWh  effective rate = {round(data["agile_cost"]/data["consumption"]/100, 2)} solar = {round(data["solar_generated"], 2)} kWh export = {round(data["export"], 2)} kWh savings = £{round((data["previous_cost"] - data["agile_cost"])/100, 2)}')

    click.echo()
    click.echo(f'Totals')
    click.echo(f'agile cost           = £{round(totals["agile_cost"]/100,2 )}')
    click.echo(f'flux cost            = £{round(totals["flux_cost"]/100, 2)}')
    click.echo(f'grid consumption     = {round(grid_consumption, 2)} kWh')
    click.echo(f'grid export          = {round(totals["export"], 2)} kWh')
    click.echo(f'actual consumption   = {round(total_consumption, 2)} kWh')
    click.echo(f'actual cost          = £{round(totals["previous_cost"]/100,2 )}')
    click.echo(f'total solar          = {round(totals["solar_generated"], 2)} kWh')
    click.echo(f'total effective rate = {round(totals["agile_cost"]/100/total_consumption, 2)} per unit')
    click.echo(f'grid effective rate  = {round(totals["agile_cost"]/100/grid_consumption, 2)} per unit')
    click.echo(f'days with 0 usage    = {days_of_zero_grid}')
    click.echo(f'solar self-use       = {round((totals["solar_generated"] - totals["export"]) / totals["solar_generated"], 2)}%')
    click.echo()
    click.echo(f'agile potential savings = £{round((totals["previous_cost"] - totals["agile_cost"])/100, 2)}')
    click.echo(f'flux potential savings  = £{round((totals["flux_total"] - totals["flux_cost"])/100, 2)} if we had been on flux in the first place...')
    # click.echo(f'{dumps(grid_usage, indent=2)}')

    if write_db:
        store_series(influx, 'solar_electricity', grid_usage)

if __name__ == '__main__':
    cmd()