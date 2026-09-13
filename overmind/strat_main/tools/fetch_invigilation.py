#!/usr/bin/env python3
"""
Economic Calendar Scraper for TradingEconomics.com
Fetches three-star impact (high importance) economic events

Usage: 

~/tradefi/retraded_1/overmind/strat_main/tools (invigilation) $ ./fetch_invigilation.py

...
Received 30 events from Supabase
Event ism services pmi already exists in Supabase
Event jolts job openings already exists in Supabase
Event building permits prel already exists in Supabase
Event building permits prel already exists in Supabase
Event housing starts already exists in Supabase
...

"""

import requests
import json
import datetime
from typing import List, Dict, Optional
import time
from urllib.parse import urljoin
import re
from bs4 import BeautifulSoup
import pytz


class TradingEconomicsCalendar:
    """
    Scraper for TradingEconomics.com economic calendar
    Focuses on three-star impact events (high importance)
    """

    def __init__(self, api_key: Optional[str] = None, range_mode: str = "recent"):
        self.api_key = api_key
        self.range_mode = range_mode
        self.base_url = "https://api.tradingeconomics.com"
        self.web_base_url = "https://tradingeconomics.com"
        self.session = requests.Session()

        # Set headers to mimic browser
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        })

        # Set base cookies for calendar access
        base_cookies = {
            'calendar-importance': '3',
            'ASP.NET_SessionId': 'shvvw0yvvgalpdapvrc452cn',
            'cal-timezone-offset': '-240',
            '_ga': 'GA1.1.245216059.1756996238',
            'TEServer': 'TEIIS',
            '_ga_SZ14JCTXWQ': 'GS2.1.s1756996238$o1$g1$t1756997334$j7$l0$h0'
        }

        # Add range-specific cookie
        range_cookies = self._get_range_cookies()
        base_cookies.update(range_cookies)

        self.session.cookies.update(base_cookies)

    def _get_range_cookies(self) -> Dict[str, str]:
        """
        Get cookies for different time range modes
        """
        range_mappings = {
            "recent": {"calendar-range": "0"},         # Recent events
            "this_month": {"calendar-range": "5"},     # This month
            "next_month": {"calendar-range": "6"},     # Next month
            "previous_month": {"calendar-range": "-3"}, # Previous month
        }

        return range_mappings.get(self.range_mode, range_mappings["recent"])

    def fetch_calendar_api(self, country: str = "united states",
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None) -> List[Dict]:
        """
        Fetch calendar data using TradingEconomics API
        Requires API key for access
        """
        if not self.api_key:
            raise ValueError("API key required for API access")

        if not start_date:
            start_date = datetime.datetime.now().strftime("%Y-%m-%d")
        if not end_date:
            end_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")

        url = f"{self.base_url}/calendar/country/{country}/{start_date}/{end_date}"
        params = {"c": self.api_key}

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()

            events = response.json()

            # Filter for three-star impact events (importance = 3)
            high_impact_events = [event for event in events
                                if event.get('Importance') == 3]

            return high_impact_events

        except requests.exceptions.RequestException as e:
            print(f"API request failed: {e}")
            return []

    def fetch_calendar_web(self, country: str = "united-states") -> List[Dict]:
        """
        Fetch calendar data by scraping the web interface
        Attempts to extract JSON data from the page
        """
        url = f"{self.web_base_url}/{country}/calendar"


        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()

            html_content = response.text

            # Look for JSON data embedded in the page
            events = self._extract_events_from_html(html_content)

            # All events are already high impact due to cookie filtering
            high_impact_events = events

            return high_impact_events

        except requests.exceptions.RequestException as e:
            print(f"Web scraping failed: {e}")
            return []

    def _extract_events_from_html(self, html_content: str) -> List[Dict]:
        """
        Extract event data from HTML content
        Looks for various patterns where calendar data might be embedded
        """
        events = []

        # Look for JSON data in script tags
        import re

        # TradingEconomics specific patterns
        patterns = [
            r'var\s+calendar_data\s*=\s*(\[.*?\]);',
            r'window\.calendar\s*=\s*(\[.*?\]);',
            r'"CalendarData":\s*(\[.*?\])',
            r'calendar:\s*(\[.*?\])',
            r'data-calendar=\'([^\']*)\'',
            r'data-events=\'([^\']*)\'',
        ]

        for pattern in patterns:
            matches = re.search(pattern, html_content, re.DOTALL)
            if matches:
                try:
                    json_data = matches.group(1)
                    events = json.loads(json_data)
                    if isinstance(events, list) and events:
                        break
                except json.JSONDecodeError:
                    continue

        # If no JSON found, try to parse table data
        if not events:
            events = self._parse_table_data(html_content)

        return events

    def _parse_table_data(self, html_content: str) -> List[Dict]:
        """
        Parse calendar events from HTML table structure
        """
        events = []

        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            # Find all calendar event rows with data attributes
            calendar_rows = soup.find_all('tr', {'data-event': True})


            for row in calendar_rows:
                event_data = self._extract_te_event_from_row(row)
                if event_data:
                    events.append(event_data)

        except Exception as e:
            print(f"HTML parsing error: {e}")

        return events

    def _extract_event_from_row(self, row) -> Optional[Dict]:
        """
        Extract event data from a table row or div
        """
        try:
            # Handle both table rows and div-based layouts
            if row.name == 'tr':
                cells = row.find_all(['td', 'th'])
                if len(cells) < 3:
                    return None

                event = {
                    'date': cells[0].get_text(strip=True) if cells[0] else '',
                    'time': cells[1].get_text(strip=True) if len(cells) > 1 else '',
                    'event': cells[2].get_text(strip=True) if len(cells) > 2 else '',
                    'country': 'United States',
                    'impact': self._extract_impact_level(row),
                    'actual': cells[3].get_text(strip=True) if len(cells) > 3 else '',
                    'previous': cells[4].get_text(strip=True) if len(cells) > 4 else '',
                    'forecast': cells[5].get_text(strip=True) if len(cells) > 5 else '',
                }
            else:
                # Handle div-based layout
                event = {
                    'date': self._extract_text_by_class(row, ['date', 'time-date']),
                    'time': self._extract_text_by_class(row, ['time', 'event-time']),
                    'event': self._extract_text_by_class(row, ['event', 'event-name', 'title']),
                    'country': 'United States',
                    'impact': self._extract_impact_level(row),
                    'actual': self._extract_text_by_class(row, ['actual', 'value-actual']),
                    'previous': self._extract_text_by_class(row, ['previous', 'value-previous']),
                    'forecast': self._extract_text_by_class(row, ['forecast', 'value-forecast']),
                }

            return event if event.get('event') else None

        except Exception as e:
            print(f"Row extraction error: {e}")
            return None

    def _extract_text_by_class(self, element, class_names: List[str]) -> str:
        """
        Extract text from element by trying multiple class names
        """
        for class_name in class_names:
            found = element.find(class_=re.compile(class_name, re.I))
            if found:
                return found.get_text(strip=True)
        return ''

    def _extract_te_event_from_row(self, row) -> Optional[Dict]:
        """
        Extract event data from TradingEconomics table row with data attributes
        """
        try:
            # Extract basic event data
            event_name = row.get('data-event', '')
            if not event_name:
                return None

            # Extract date from td class attribute
            date_str = ''
            time_str = ''

            date_td = row.find('td', class_=re.compile(r'202\d-\d\d-\d\d'))
            if date_td:
                date_classes = [c for c in date_td.get('class', []) if c.startswith('202')]
                if date_classes:
                    date_str = date_classes[0]  # e.g., '2025-09-03'

            # Extract time from span
            time_cell = row.find('span', class_=re.compile(r'calendar-date-\d+'))
            if time_cell:
                time_str = time_cell.get_text(strip=True)  # e.g., '10:00 AM'

            # Convert to UTC timestamp
            utc_timestamp = self._convert_ny_to_utc(date_str, time_str)

            # Extract values
            actual_span = row.find('span', {'id': 'actual'})
            actual = actual_span.get_text(strip=True) if actual_span else ''

            previous_span = row.find('span', {'id': 'previous'})
            previous = previous_span.get_text(strip=True) if previous_span else ''

            forecast_span = row.find('a', {'id': 'forecast'})
            forecast = forecast_span.get_text(strip=True) if forecast_span else ''

            event = {
                'timestamp_utc': utc_timestamp,
                'event': event_name,
                'country': row.get('data-country', 'US'),
                'category': row.get('data-category', ''),
                'symbol': row.get('data-symbol', ''),
                'impact': 3,  # All events are high impact since we filtered with cookie
                'actual': actual,
                'previous': previous,
                'forecast': forecast,
            }

            return event

        except Exception as e:
            print(f"TE row extraction error: {e}")
            return None

    def _convert_ny_to_utc(self, date_str: str, time_str: str) -> str:
        """
        Convert NY time to UTC timestamp in ISO 8601 format for Supabase compatibility
        """
        if not date_str or not time_str:
            return ''

        try:
            # Parse the date and time
            date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            time_obj = datetime.datetime.strptime(time_str, '%I:%M %p').time()

            # Combine date and time
            ny_tz = pytz.timezone('America/New_York')
            local_dt = ny_tz.localize(datetime.datetime.combine(date_obj.date(), time_obj))

            # Convert to UTC
            utc_dt = local_dt.astimezone(pytz.UTC)

            # Return in ISO 8601 format with timezone (what Supabase expects)
            return utc_dt.isoformat()

        except Exception as e:
            print(f"Time conversion error: {e}")
            return f"{date_str.replace('-', '')} {time_str}"

    def _extract_impact_level(self, row) -> int:
        """
        Extract impact level from row (looking for star indicators)
        """
        # Look for star indicators in various formats
        row_html = str(row)

        # Count star symbols
        star_count = row_html.count('') + row_html.count('*')

        # Look for impact class indicators
        if 'high' in row_html.lower() or 'red' in row_html.lower():
            return 3
        elif 'medium' in row_html.lower() or 'orange' in row_html.lower():
            return 2
        elif 'low' in row_html.lower() or 'yellow' in row_html.lower():
            return 1

        # Default based on star count
        return min(star_count, 3) if star_count > 0 else 1

    def _is_high_impact(self, event: Dict) -> bool:
        """
        Check if event is high impact (three-star)
        """
        impact = event.get('impact', event.get('Importance', 0))
        return impact == 3

    def get_recent_events(self, days_back: int = 7, days_forward: int = 30) -> List[Dict]:
        """
        Get recent and upcoming high-impact events
        """
        start_date = (datetime.datetime.now() - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
        end_date = (datetime.datetime.now() + datetime.timedelta(days=days_forward)).strftime("%Y-%m-%d")

        # Try API first if available
        if self.api_key:
            try:
                return self.fetch_calendar_api(start_date=start_date, end_date=end_date)
            except Exception as e:
                print(f"API fetch failed: {e}, falling back to web scraping")

        # Fall back to web scraping
        return self.fetch_calendar_web()

    def print_events(self, events: List[Dict]):
        """
        Print formatted event information
        """
        if not events:
            print("No three-star impact events found")
            return

        print(f"\n=== THREE-STAR IMPACT ECONOMIC EVENTS ({len(events)} found) ===")
        print("-" * 80)

        for event in events:
            timestamp_utc = event.get('timestamp_utc', 'N/A')
            event_name = event.get('event', '')
            country = event.get('country', 'US')
            actual = event.get('actual', 'N/A')
            previous = event.get('previous', 'N/A')
            forecast = event.get('forecast', 'N/A')

            print(f"Time: {timestamp_utc}")
            print(f"Event: {event_name}")
            print(f"Country: {country}")
            print(f"Actual: {actual} | Previous: {previous} | Forecast: {forecast}")
            print("-" * 60)




    def write_events_to_supabase(self, events: List[Dict]):
        """
        Write events to Supabase
        """
        PROJECT_ID = "lolgjbnpttwqdqtmgwux"
        API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxvbGdqYm5wdHR3cWRxdG1nd3V4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTY0Nzg1NDcsImV4cCI6MjA3MjA1NDU0N30.ZlZ2XwdqqEuStrxwUT6VInIRRxdwVCwTyqT9UlOtV0w"

        BASE = "https://{}.supabase.co/rest/v1".format(PROJECT_ID)
        TABLE = "invigilator_1"
        #API_KEY = os.environ["SUPABASE_KEY"]           # anon, user JWT, or service_role
        AUTH = "Bearer {}".format(API_KEY)


        # Nothing to do.
        if len(events) == 0:
            return


        # Step 1, find the first and last events and their timestamps.

        first_time = events[0]['timestamp_utc']
        last_time = events[-1]['timestamp_utc']

        basic_headers = {
            "apikey": API_KEY,
            "Authorization": AUTH,
        }


        params = [
            ("select", "*"),
            ("event_time", f"gte.{first_time}"),
            ("event_time", f"lte.{last_time}"),
        ]

        r = requests.get(f"{BASE}/{TABLE}", headers=basic_headers, params=params)


        r_json = r.json()
        # Go through and check which events should actually go through.
        print("Received {} events from Supabase".format(len(r_json)))


        should_keep_events = []

        for one_event in events:
            should_keep = True
            for one_supabase_event in r_json:
                if one_event['timestamp_utc'] == one_supabase_event['event_time'] and one_event["event"] == one_supabase_event["event_name"]:
                    # Comment this out later, this is just for debugging at the start.
                    print(f"Event {one_event['event']} already exists in Supabase")
                    should_keep = False
                    break
                #print("Comparing {} vs {} ({}) and {} vs {} ({})".format(
                #    one_event['timestamp_utc'], one_supabase_event['event_time'], one_event['timestamp_utc']== one_supabase_event['event_time'], one_event['event'], one_supabase_event['event_name'], one_event['event']== one_supabase_event['event_name']
                #))


            if should_keep:
                should_keep_events.append(one_event)


        print("Should_keep_events is len {}".format(len(should_keep_events)))


        # OK, now go through and write the events to supabase. No biggie.
        write_header = {
            "apikey": API_KEY,
            "Authorization": AUTH,
            "Content-Type": "application/json"
        }


        for one_event in should_keep_events:
            insert_data = {
                "event_time": one_event['timestamp_utc'],
                "event_name": one_event['event'],

            }
            print("About to write event to Supabase: {}".format(insert_data))


            r = requests.post(
                f"{BASE}/{TABLE}",
                headers=write_header,
                data=json.dumps(insert_data))
            print("Request result: {}".format(r.status_code))




def main():
    """
    Main function to fetch and display three-star impact events
    """
    import argparse

    parser = argparse.ArgumentParser(description='Fetch three-star impact economic events from TradingEconomics')
    parser.add_argument('--mode', choices=['recent', 'this_month', 'next_month', 'previous_month'],
                        default='recent', help='Time range mode (default: recent)')
    parser.add_argument('--api-key', help='TradingEconomics API key (optional)')

    args = parser.parse_args()

    # Initialize calendar scraper with selected mode
    calendar = TradingEconomicsCalendar(api_key=args.api_key, range_mode=args.mode)

    mode_names = {
        'recent': 'Recent',
        'this_month': 'This Month',
        'next_month': 'Next Month',
        'previous_month': 'Previous Month'
    }

    print(f"Fetching three-star impact events - {mode_names[args.mode]} mode...")

    try:
        # Fetch events for the specified range
        events = calendar.get_recent_events()
        # Let's start testing the invig situation.

        calendar.write_events_to_supabase(events)


        # Print results
        #calendar.print_events(events)

        # Additional info
        print(f"\nTotal events fetched: {len(events)}")
        print(f"Mode: {mode_names[args.mode]}")
        print("Note: This scraper focuses on three-star (high impact) events only")

        if not calendar.api_key:
            print("\nFor more reliable data access, consider using TradingEconomics API")
            print("Set API_KEY environment variable or pass to TradingEconomicsCalendar(api_key='your_key')")

    except Exception as e:
        print(f"Error fetching calendar events: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
