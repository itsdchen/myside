#! /usr/bin/env python

import os
import getpass

import datetime
from datetime import datetime as dt
import pytz
from dateutil import parser, tz
from zoneinfo import ZoneInfo


"""
Some basic functions to help us with dates.
Most of these are going to be a basic wrapper for datetime calls.


Just because I don't remember the native calls.

Let's get the weekday/weekends.


dates_method:

ALLDAYS = all days
WEEKDAYS = only choose weekdays
WEEKENDS = only choose weekends

"""

date_fmt = "%Y%m%d"


def blacklist(dates_list, blacklist_dates):
    result_set = set(dates_list).difference(set(blacklist_dates))
    result_list = list(result_set)
    result_list.sort()
    return result_list


# Gives you the string dates between A and B.
def dates_list(start_str, end_str, dates_method="ALL_DAYS"):
    start_d = dt.strptime(start_str, date_fmt)
    end_d = dt.strptime(end_str, date_fmt)
    lst_days = [
        start_d + datetime.timedelta(days=x) for x in range((end_d - start_d).days)
    ]

    str_days = []
    for one_day in lst_days:
        # Check the dates method.
        if dates_method == "ALLDAYS":
            pass
        elif dates_method == "WEEKDAYS":
            if one_day.weekday() in [5, 6]:
                continue
        elif dates_method == "WEEKENDS" or dates_method == "WEEKEND":
            if one_day.weekday() not in [5, 6]:
                continue

        str_days.append(one_day.strftime(date_fmt))

    # days that are crazy or unusual.
    # 0805 is a crazy high volume day, almost 200B on binance, it'll just slow all training down.
    # 20260427-20260430: capture broke at a misspelled DKNG entry — everything alphabetically
    # after it was lost (incl. xyz:GOOGL, xyz:NVDA). Sims on these days would be partial.
    PERM_BLACKLIST = ["20240805", "20241004", "20241011", "20241013", "20241014", "20241015", "20241029", "20241114", "20250108", "20250109", "20250110", "20251225", "20260101", "20260427", "20260428", "20260429", "20260430"]

    filtered_days = [a_day for a_day in str_days if a_day not in PERM_BLACKLIST]

    return filtered_days

def t_to_secs(t_str):
    dt = parser.parse(
        t_str,
        tzinfos={"ET": tz.gettz("America/New_York")}
    )

    seconds_since_epoch = int(dt.timestamp())
    return seconds_since_epoch

# Anyway, tells us which days we can use. Great.
def dates_avail(
    start_str, end_str, symbol, market, data_dir="{}/tardis_datasets/gzpbf".format(os.getenv("HOME")),
    dates_method="ALL_DAYS"
):
    all_dates = dates_list(start_str, end_str, dates_method=dates_method)

    good_dates = []
    bad_dates = []
    # Now go through them and figure out if the datafiles exist.
    for one_date in all_dates:
        # Just use the bookTicker as a proxy.
        # Later on we can check all streams. Oh well.
        import mktdata
        channel_name = mktdata.books_to_channels[market][0]

        # Do symbol substitution.
        sym_str = symbol.replace("/", "_")

        data_path = os.path.join(
            data_dir, market, "{}_{}_{}.gzpbf".format(sym_str, channel_name, one_date)
        )
        if os.path.exists(data_path):
            good_dates.append(one_date)
        else:
            bad_dates.append(one_date)

    return {"good_dates": good_dates, "bad_dates": bad_dates}



# Format is "YYYYMMDD HH:MM:SS"
# Output: unix epoc seconds.
def timestr_to_secs(dt_string):
    dt_part, tz_part = dt_string.rsplit(' ', 1)

    dt_naive = dt.strptime(dt_part, "%Y%m%d %H:%M:%S")
    timezone = pytz.timezone(tz_part)
    dt_aware = timezone.localize(dt_naive)
    epoch_seconds = dt_aware.timestamp()
    return epoch_seconds


# Times we tend to start processes.
daily_start_times = {
    "CMEStart": "18:00:00 America/New_York",
    "USStart": "9:30:00 America/New_York",
    "CMEHourOffStart": "17:00:00 America/New_York",
}


# Times we tend to end processes.
daily_end_times = {
    # When normal
    "CMEEnd": "17:00:00 America/New_York",
    "USEnd": "16:00:00 America/New_York",
    "CMEHourOffEnd": "18:00:00 America/New_York",
    "PKDayEnd": "18:00:00 America/New_York"
}


def get_start_time(label, date):
    if label not in daily_start_times:
        raise ValueError("Invalid label: {}".format(label))

    # Get the start time for the given label.
    start_time = daily_start_times[label]
    start_time_str = "{} {}".format(date, start_time)

    return start_time_str

def get_end_time(label, date):
    if label not in daily_end_times:
        raise ValueError("Invalid label: {}".format(label))

    # Get the end time for the given label.
    end_time = daily_end_times[label]
    end_time_str = "{} {}".format(date, end_time)

    return end_time_str




# Two modes,
# Checks if it's today or tomorrow in eastern.
# It's maybe easier in utc, but tbh It's just easier for me to
# reason about this in eastern time, on the day of.
def get_date_easy(date_label):
    eastern_t = dt.now(ZoneInfo("America/New_York"))

    if date_label == "TODAY":
        # no-op
        pass
    elif date_label == "TOMORROW":
        eastern_t = eastern_t + datetime.timedelta(days=1)
    elif date_label == "TOMORROW2":
        eastern_t = eastern_t + datetime.timedelta(days=2)
    elif date_label == "TOMORROW3":
        eastern_t = eastern_t + datetime.timedelta(days=3)
    else:
        raise ValueError("Invalid value for date_label: {}".format(date_label))

    day_str = eastern_t.strftime(date_fmt)
    #print(day_str)
    return day_str

