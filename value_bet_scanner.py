#!/usr/bin/env python3
"""
Value Bet Scanner - OddsPapi API Integration
Features:
- Multi-API key support with rotation
- Fractional Kelly stake calculation
- Median-based EV calculation (more accurate)
- Direct betslip links to outcomes
- Telegram commands (/run, /stop, /profit, /set, /keys, /bankroll, /manueel)
- Manual bet entry via Telegram conversation flow
- Settlement tracking via API
- Google Sheets logging with monthly tabs
"""
import re
import requests
import json
import time
import logging
import threading
import statistics
import subprocess
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
import os
from pathlib import Path
from collections import defaultdict
from pprint import pprint
from mapping import MARKETS as mapping

from dotenv import load_dotenv
load_dotenv()

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    GOOGLE_SHEETS_AVAILABLE = True
except ImportError:
    GOOGLE_SHEETS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('value_bet_scanner.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Column headers for the bet log sheet
SHEET_HEADERS = [
    'Settlement', 'Datum', 'Start wedstrijd', 'Event fixture', 'Outcome id', 'Match',
    'Sport', 'Market', 'Outcome', 'Land / Tournooi', 'League',
    'Soft Book', 'Odds overzicht (soft)', 'Sharp Ref (mediaan)',
    'EV %', 'Win Prob', 'Stake Amount', 'Kelly %',
    'Betslip', 'Mogelijke winst'
]

class ApiKeyManager:
    """Manager for multiple API keys with rotation and rate limiting"""

    def __init__(self, api_keys: List[str], requests_per_key: int = 250):
        self.api_keys = api_keys if isinstance(api_keys, list) else [api_keys]
        self.requests_per_key = requests_per_key
        self.current_index = 0
        self.key_usage = {key: 0 for key in self.api_keys}
        self.key_errors = {key: 0 for key in self.api_keys}
        self.total_requests = 0
        self._lock = threading.Lock()
    

    def get_next_key(self) -> str:
        with self._lock:
            for _ in range(len(self.api_keys)):
                key = self.api_keys[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.api_keys)
                if self.key_usage[key] < self.requests_per_key:
                    return key
            return min(self.api_keys, key=lambda k: self.key_errors[k])

    def record_request(self, api_key: str):
        with self._lock:
            self.key_usage[api_key] = self.key_usage.get(api_key, 0) + 1
            self.total_requests += 1

    def record_error(self, api_key: str):
        with self._lock:
            self.key_errors[api_key] = self.key_errors.get(api_key, 0) + 1

    def get_status(self) -> Dict:
        with self._lock:
            return {
                'total_requests': self.total_requests,
                'keys': [
                    {
                        'key': key[:8] + '...' if len(key) > 8 else key,
                        'usage': self.key_usage.get(key, 0),
                        'limit': self.requests_per_key,
                        'errors': self.key_errors.get(key, 0),
                        'remaining': max(0, self.requests_per_key - self.key_usage.get(key, 0))
                    }
                    for key in self.api_keys
                ],
                'total_remaining': sum(
                    max(0, self.requests_per_key - self.key_usage.get(key, 0))
                    for key in self.api_keys
                )
            }


@dataclass
class ValueBet:
    """Represents a detected value bet"""
    fixture_id: str
    participant1: str
    participant2: str
    start_time: str
    tournament_name: str
    category_name: str
    market: str
    market_id: str
    outcome: str
    outcome_id: str
    sharp_bookmaker: str
    sharp_odds: float          # median sharp reference
    soft_bookmaker: str        # best soft book for this bet
    soft_odds: float           # odds at best soft book
    soft_bookmaker_odds: Dict[str, float]  # odds at ALL soft books for this outcome
    ev_percentage: float
    win_probability: float
    sport: str
    stake_amount: float
    bankroll: float
    kelly_fraction: float
    possible_profit: float
    timestamp: str
    betslip_url: Optional[str] = None
    settlement_status: str = "PENDING"

    def to_dict(self) -> Dict:
        # Build a compact odds overview string: "cashpoint:2.10 unibet:2.05 ..."
        odds_str = '  '.join(
            f"{bk} @ {o:.2f}"
            for bk, o in sorted(self.soft_bookmaker_odds.items(), key=lambda x: -x[1])
        )

        return {
            'Settlement': self.settlement_status,
            'Datum': self.timestamp,
            'Start wedstrijd': self.start_time,
            'Event fixture': self.fixture_id,
            'Outcome id': self.outcome_id,
            'Match': f"{self.participant1} - {self.participant2}",
            'Sport': self.sport,
            'Market': self.market,
            'Outcome': self.outcome,
            'Land / Tournooi': self.category_name,
            'League': self.tournament_name,
            'Soft Book': f"{self.soft_bookmaker} @ {self.soft_odds}",
            'Odds overzicht (soft)': odds_str,
            'Sharp Ref (mediaan)': round(self.sharp_odds, 4),
            'EV %': round(self.ev_percentage / 100, 4),
            'Win Prob': round(self.win_probability, 4),
            'Stake Amount': round(self.stake_amount, 2),
            'Kelly %': round(self.kelly_fraction, 4),
            'Betslip': self.betslip_url or '',
            'Mogelijke winst': round(self.possible_profit, 2)
        }


class OddsPapiClient:
    """Client for OddsPapi API v4 with multi-key support"""

    BASE_URL = "https://api.oddspapi.io/v4"


    SOFT_BOOKMAKERS = [
        'betcenter.be', 'unibet.be', 'betano', 'goldenpalacesports.be',
        'bwin.be', 'napoleonsports.be', 'bet365', 'bcgame'

        #ladbrokes.be
        #betcenter.be, 
        # bingoal.be, 
        # betfirst.be, -> betsson?
        #goldenpalacesports.be, 
        #starcasino.be -> starsport.be
        #cashpoint.be --> betcenter.be
    ]

    # Sharp books used only for median reference, NOT as bet targets
    SHARP_BOOKMAKERS = [
        'pinnacle', 'sbobet', 'bwin.be', 'betonline.ag', 'ps3838', 'smarkets'
    ]

    def __init__(self, api_keys, settlements, requests_per_key: int = 250):
        self.key_manager = ApiKeyManager(api_keys, requests_per_key)
        self.session = requests.Session()
        self.settlements = settlements
        self.api_keys = api_keys


    def rotate_ip(self):
        try:
            subprocess.run(
                ["bash", "/home/pi/services/BetScanner/rotate_vpn_on_call.sh"],
                check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"VPN-script faalde: {e}")
        except KeyboardInterrupt:
            print("Programma onderbroken door gebruiker")


    def _make_request(self, endpoint: str, params: Dict = None) -> requests.Response:

        api_key = self.key_manager.get_next_key()
        if params is None:
            params = {}
        params['apiKey'] = api_key

        try:
            response = self.session.get(
                f"{self.BASE_URL}/{endpoint}",
                params=params,
                timeout=(10, 60)
            )

    
            error = response.json().get("error", None)

            if error:
                if response.status_code == 429:
                    return self._make_request(endpoint, params)

                if response.status_code == 404:
                    if error.get("message") == "No scores found for the specified fixture.":
                        logger.info(f"No scores found for: {response.url}")
                        return None

                if response.status_code == 403:
                    if error.get("message") == "Forbidden":
                        logger.warning("Forbidden 403 -> rotate IP address")
                        
                        self.rotate_ip()
                        time.sleep(5)
                        return self._make_request(endpoint, params)

                else:
                    if error.get("message") == "No scores found for the specified fixture.":
                        return None
                    if error.get("message") == "Invalid fixture ID provided.":
                        return None

            return response
        
        except Exception as e:
            return response
            
    


    def get_sports(self) -> List[Dict]:
        try:
            response = self._make_request("sports")
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching sports: {e}")
            return []

    def get_tournaments(self, sport_id: int = 10) -> List[Dict]:
        try:

            response = self._make_request("tournaments", {'sportId': sport_id})

            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.ConnectionError as e:
            logger.warning(
                f"Connection reset {e}"
            )
            time.sleep(3)
            return self.get_tournaments(sport_id)

        except requests.exceptions.ReadTimeout:
            logger.warning("Oddspapi timeout, retrying...")
            time.sleep(5)
            return self.get_tournaments(sport_id)

        except Exception as e:
            logger.error(f"Error fetching tournaments: {e}")
            return []

    def get_markets(self):
        response = self._make_request("markets")

        return response.json()


    def get_fixture(self, fixture_id):

        params = {
                'fixtureId': fixture_id
                }
        
        try:
            response = self._make_request("fixture", params)
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.ConnectionError as e:
            logger.warning(
                f"Connection reset {e}"
            )
            time.sleep(3)
            return self.get_fixture(fixture_id)
        
        except requests.exceptions.ReadTimeout:
            logger.warning("Oddspapi timeout, retrying...")
            time.sleep(5)
            return self.get_fixture(fixture_id)
            
        except Exception as e:
            return []



    def get_fixtures(self, tournament_id: Optional[int] = None, sport_id: int = 10,
                      days_ahead: int = 7, has_odds: bool = True) -> List[Dict]:
        
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        params = {
            'sportId': sport_id,
            'from': tomorrow.isoformat(),
            'to': (tomorrow + timedelta(days=days_ahead)).isoformat(),
        }
        if tournament_id:
            params['tournamentId'] = tournament_id
        if has_odds:
            params['hasOdds'] = 'true'

        try:
            response = self._make_request("fixtures", params)
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.ConnectionError as e:
            logger.warning(
                f"Connection reset {e}"
            )
            time.sleep(3)
            return self.get_fixtures(tournament_id, days_ahead, has_odds)
        
        except requests.exceptions.ReadTimeout:
            logger.warning("Oddspapi timeout, retrying...")
            time.sleep(5)
            return self.get_fixtures(tournament_id, days_ahead, has_odds)
            
        except Exception as e:

            return []

    def get_odds(self, fixture_id: str) -> Dict:
        try:
            response = self._make_request("odds", {'fixtureId': fixture_id})
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.ConnectionError as e:
            logger.warning(
                f"Connection reset {e}"
            )
            time.sleep(3)
            return self.get_odds(fixture_id)

        except requests.exceptions.ReadTimeout:
            logger.warning("Oddspapi timeout, retrying...")
            time.sleep(5)
            return self.get_odds(fixture_id)

        except Exception as e:
            logger.error(f"Error fetching odds for {fixture_id}: {e}")
            return {}
        
  
    def get_scores(self, fixture_ids: List[str]) -> List[Dict]:
        if not fixture_ids:
            return []

   
        scores = []
        try:
            for id in fixture_ids:
                response = self._make_request("scores", {'fixtureId': id})
                if response is None:
                    continue
                
                scores.append(response.json())

            return scores \
                if scores is not None else None


        except requests.exceptions.ConnectionError as e:
            logger.warning(
                f"Connection reset {e}"
            )
            time.sleep(3)
            return self.get_scores(fixture_ids)

        except requests.exceptions.ReadTimeout:
            logger.warning("Oddspapi timeout, retrying...")
            time.sleep(5)
            return self.get_scores(fixture_ids)


    def extract_odds_from_market(
        self,
        bookmaker_data: Dict,
        market_ids: List[str]
    ) -> Dict[str, Dict[str, float]]:

        odds = {}

        markets = bookmaker_data.get('markets', {})

        for market_id in market_ids:
            market = markets.get(market_id, {})
            outcomes = market.get('outcomes', {})

            market_odds = {}

            for outcome_id, outcome_data in outcomes.items():
                players = outcome_data.get('players', {})

                if '0' in players:
                    price = players['0'].get('price')

                    if price:
                        market_odds[outcome_id] = price

            if market_odds:
                odds[market_id] = market_odds

        return odds


    def get_outcome_betslip_url(self, bookmaker_data: Dict, outcome_id: str, best_bookmaker) -> Optional[str]:
        fixture_path = bookmaker_data.get('fixturePath', '') \
            if best_bookmaker in ("napoleonsports.be", "unibet.be", "bwin.be") else None
        
        if not fixture_path:
            return None

        markets = bookmaker_data.get('markets', {})
        market_101 = markets.get('101', {})
        outcomes = market_101.get('outcomes', {})
        outcome_data = outcomes.get(outcome_id, {})
        players = outcome_data.get('players', {})
        player_0 = players.get('0', {})
 
        if fixture_path:
            return fixture_path
        
        return None


class ValueBetCalculator:
    """Calculate value bets using median sharp reference and fractional Kelly"""


    def __init__(self, min_ev_threshold: float = 20.0, kelly_fraction: float = 0.25, min_win_prob: float = 8.0):
        self.min_ev_threshold = min_ev_threshold
        self.min_win_prob = min_win_prob
        self.kelly_fraction = kelly_fraction
        self.odds_client = None

    def set_odds_client(self, client: OddsPapiClient):
        self.odds_client = client

    def calculate_implied_probability(self, odds: float) -> float:
        return 1 / odds if odds > 0 else 0

    def calculate_ev(self, soft_odds: float, sharp_odds: float) -> float:
        if sharp_odds <= 0 or soft_odds <= 0:
            return 0
        true_probability = self.calculate_implied_probability(sharp_odds)
        return ((true_probability * soft_odds) - 1) * 100

    def calculate_kelly(self, probability: float, odds: float) -> float:
        if odds <= 1:
            return 0
        b = odds - 1
        q = 1 - probability
        kelly = (probability * b - q) / b
        return max(0, kelly)

    def calculate_stake(self, probability: float, odds: float, bankroll: float,
                        fraction: float = 0.25) -> Tuple[float, float]:
        full_kelly = self.calculate_kelly(probability, odds)
        fractional_kelly = full_kelly * fraction
        stake_amount = bankroll * fractional_kelly
        return stake_amount, fractional_kelly

    def calculate_median_odds(self, odds_list: List[float]) -> float:
        if not odds_list:
            return 0
        return statistics.median(odds_list)

    def analyze_fixture(self, fixture: Dict, odds_data: Dict, bankroll: float) -> List[ValueBet]:
        value_bets = []
        bookmaker_odds = odds_data.get('bookmakerOdds', {})

        sport_id = str(fixture.get('sportId'))
        sport_data = mapping.get(sport_id, {})

        if not sport_data:
            return value_bets

        sport_name = next(iter(sport_data))
        sport_markets = sport_data[sport_name]
        market_ids = list(sport_markets.keys())
             
        # Collect median odds from sharp bookmakers
        sharp_prices_by_outcome: Dict[str, Dict[str, Dict[str, float]]] = {}
        sharp_bookies_found = 0
        for sharp in OddsPapiClient.SHARP_BOOKMAKERS:
            
            if sharp not in bookmaker_odds:
                continue

            sharp_bookies_found += 1
            if sharp_bookies_found >= len(OddsPapiClient.SHARP_BOOKMAKERS) - 3:
                markets = self.odds_client.extract_odds_from_market(
                    bookmaker_odds[sharp],
                    market_ids
                    )
                
                for market_id, market_odds in markets.items():
                    for outcome_id, price in market_odds.items():
                        sharp_prices_by_outcome \
                            .setdefault(market_id, {}) \
                            .setdefault(outcome_id, {})[sharp] = price \


        if not sharp_prices_by_outcome:
            return value_bets


        median_sharp_odds = {}

        for market_id, outcomes in sharp_prices_by_outcome.items():
            median_sharp_odds[market_id] = {}

            for outcome_id, prices in outcomes.items():
                sharp_odds_list = list(prices.values())
                median_sharp_odds[market_id][outcome_id] = \
                self.calculate_median_odds(sharp_odds_list)
             
      
        soft_odds_by_outcome: Dict[str, Dict[str, float]] = {}
        for soft_book in OddsPapiClient.SOFT_BOOKMAKERS:
            if soft_book not in bookmaker_odds:
                continue

            book_odds = self.odds_client.extract_odds_from_market(
                bookmaker_odds[soft_book], market_ids
            )

            for market_id, market_odds in book_odds.items():
                for outcome_id, price in market_odds.items():
                    soft_odds_by_outcome \
                        .setdefault(market_id, {}) \
                        .setdefault(outcome_id, {})[soft_book] = price \


        # Find value: for each outcome pick the best soft book

        for market_id, outcomes in median_sharp_odds.items():
            for outcome_id, median_sharp in outcomes.items():

                all_soft = (
                    soft_odds_by_outcome
                    .get(market_id, {})
                    .get(outcome_id, {})
                )

                if not all_soft:
                    continue

                best_book = max(all_soft, key=lambda b: all_soft[b])

                if (market_id == '12245' or market_id == '12247') \
                    and best_book == 'bet365':
                    continue
            
                if (market_id == '181' or market_id == '182') \
                    and best_book == 'bc.game':
                    continue


                best_odds = all_soft[best_book]
                ev = self.calculate_ev(best_odds, median_sharp)
                win_prob = self.calculate_implied_probability(median_sharp)

                if not ev <= 100:
                    continue

                if (ev >= self.min_ev_threshold and win_prob * 100 >= self.min_win_prob):
                    start_data = fixture.get('startTime', '').split('T')
                    start_date, start_time = start_data[0], start_data[1][:5]

                    stake_amount, kelly_pct = self.calculate_stake(
                        win_prob, best_odds, bankroll, self.kelly_fraction
                    )
                    betslip_url = self.odds_client.get_outcome_betslip_url(
                        bookmaker_odds[best_book], outcome_id, best_book
                    )

                    market_info = sport_markets.get(market_id)

                    if not market_info:
                        continue

                    market_name = next(iter(market_info))
                    outcomes = market_info[market_name]

                    outcome_name = outcomes.get(
                        outcome_id,
                        "Unknown"
                    )

                    value_bets.append(ValueBet(
                        fixture_id=fixture.get('fixtureId', ''),
                        participant1=fixture.get('participant1Name', 'Unknown'),
                        participant2=fixture.get('participant2Name', 'Unknown'),
                        start_time=f"{start_date} {start_time}",
                        tournament_name=fixture.get('tournamentName', 'Unknown'),
                        category_name=fixture.get('categoryName', 'Unknown'),
                        market=market_name,
                        market_id=market_id,
                        outcome = outcome_name,
                        outcome_id=outcome_id,
                        sharp_bookmaker='median',
                        sharp_odds=median_sharp,
                        soft_bookmaker=best_book,
                        soft_odds=best_odds,
                        soft_bookmaker_odds=dict(all_soft),
                        ev_percentage=ev,
                        sport=sport_name,
                        win_probability=win_prob,
                        stake_amount=stake_amount,
                        bankroll=bankroll,
                        kelly_fraction=kelly_pct,
                        timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        betslip_url=betslip_url,
                        possible_profit= best_odds * stake_amount - stake_amount
                    ))

        return value_bets


class GoogleSheetsManager:
    """Manage Google Sheets with monthly tab support"""

    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    HEADER_ROW = SHEET_HEADERS

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        self.spreadsheet_id = spreadsheet_id
        self.available = False
        self.service = None
        self._sheet_lock = threading.Lock()
        # In-process cache: set of sheet names known to exist
        self._known_sheets: Optional[set] = None
        self.first_data_row = 12

        if not GOOGLE_SHEETS_AVAILABLE:
            logger.warning("Google Sheets libraries not installed")
            return

        try:
            credentials = Credentials.from_service_account_file(credentials_path, scopes=self.SCOPES)
            self.service = build('sheets', 'v4', credentials=credentials)
            self.available = True
            logger.info("Google Sheets client initialized")
        except Exception as e:
            logger.error(f"Error initializing Google Sheets: {e}")


    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def update_main_sheet_totals(self, main_sheet="Dashboard"):

        try:
            spreadsheet = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()

            sheets = spreadsheet.get("sheets", [])

            pattern = re.compile(r"^\d{4}-\d{2}$")

            monthly_sheets = [
                s["properties"]["title"]
                for s in sheets
                if pattern.match(s["properties"]["title"])
            ]

            len_monthly_sheets = len(monthly_sheets)

            # 5 rijen voor B2:B6 en 5 rijen voor D2:D6
            total_B = [0, 0, 0, 0, 0, 0]
            total_D = [0, 0, 0, 0, 0]


            for sheet_name in monthly_sheets:

                # Kolom B ophalen
                result_B = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{sheet_name}!C2:C7",
                    valueRenderOption="UNFORMATTED_VALUE"
                ).execute()

                values_B = result_B.get("values", [])

                for i, row in enumerate(values_B):
                    if i >= len(total_B):
                        break

                    if len(row) == 0:
                        continue

                    try:
                        total_B[i] += float(row[0])
                    except (ValueError, TypeError):
                        continue

                # Kolom D ophalen
                result_D = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{sheet_name}!E2:E7",
                    valueRenderOption="UNFORMATTED_VALUE"
                ).execute()

                values_D = result_D.get("values", [])

                for i, row in enumerate(values_D):
                    if i >= len(total_D):
                        break

                    if len(row) == 0:
                        continue

                    try:
                        total_D[i] += float(row[0])
                    except (ValueError, TypeError):
                        continue

            # Resultaat samenvoegen voor Dashboard A2:D6
            output = []
            for i in range(5):
                output.append([
                    "",              # A behoudt beschrijving
                    total_B[i],      # B totalen
                    "",              # C behoudt beschrijving
                    total_D[i]       # D totalen
                ])


            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{main_sheet}!F14:F19",
                valueInputOption="USER_ENTERED",
                body={
                    "values": [[value] for value in total_B]
                }
            ).execute()

            # Schrijf enkel D2:D6
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{main_sheet}!H14:H18",
                valueInputOption="USER_ENTERED",
                body={
                    "values": [[value] for value in total_D]
                }
            ).execute()


            # Aantal maandbladen opslaan
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{main_sheet}!C17",
                valueInputOption="USER_ENTERED",
                body={
                    "values": [[len_monthly_sheets]]
                }
            ).execute()


            logger.info("Main sheet totals updated")
            
        except Exception as e:
            logger.error(f"Error updating totals: {e}, trying again")
            time.sleep(5)
            return self.update_main_sheet_totals()

    

    def _fetch_sheet_meta(self) -> List[Dict]:
        """Single API call — returns the sheets array from spreadsheet metadata."""
        meta = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id
        ).execute()
        return meta.get('sheets', [])

    def _refresh_known_sheets(self) -> Dict[str, int]:
        """Returns {title: sheetId} and updates _known_sheets cache."""
        sheets = self._fetch_sheet_meta()
        mapping = {s['properties']['title']: s['properties']['sheetId'] for s in sheets}
        self._known_sheets = set(mapping.keys())
        return mapping

    def _duplicate_sheet(self, source_id: int, new_name: str) -> bool:
        """Copy sheet by id to new_name and clear data rows (keep header)."""
        try:
            self.service.spreadsheets().sheets().copyTo(
                spreadsheetId=self.spreadsheet_id,
                sheetId=source_id,
                body={'destinationSpreadsheetId': self.spreadsheet_id}
            ).execute()
            # The copy lands as "Copy of TEMPLATE" — look it up fresh
            mapping = self._refresh_known_sheets()
            copy_name = next(
                (t for t in mapping if t.startswith('Kopie van ') and t not in (new_name,)),
                None
            )
            if copy_name is None:
                logger.error("Could not find the copied sheet to rename")
                return False
            copy_id = mapping[copy_name]
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={'requests': [{'updateSheetProperties': {
                    'properties': {'sheetId': copy_id, 'title': new_name},
                    'fields': 'title'
                }}]}
            ).execute()
            # Clear data rows, keep header row
            self.service.spreadsheets().values().clear(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{new_name}'!A{self.first_data_row}:Z"
            ).execute()
            # Update cache
            self._known_sheets.discard(copy_name)
            self._known_sheets.add(new_name)
            return True
        except Exception as e:
            logger.error(f"Error duplicating sheet: {e}")
            return False

    # ------------------------------------------------------------------
    # Monthly sheet management
    # ------------------------------------------------------------------

    

    def get_or_create_monthly_sheet(self, year: int = None, month: int = None) -> str:
        """
        Return the sheet name for the given month (default: current month).
        Creates exactly one new sheet from the 'TEMPLATE' tab if needed.
        Sheet names follow the pattern 'YYYY-MM' (e.g. '2026-07').
        """
        if not self.available:
            return 'Sheet1'

        now = datetime.now()
        year = year or now.year
        current_month = now.month

        if month is None:
            sheet_name = f"{year}-{current_month:02d}"

        else:
            sheet_name = f"{year}-{month}"

        with self._sheet_lock:
            # Fast path: already in local cache
            if self._known_sheets is not None and sheet_name in self._known_sheets:
                return sheet_name

            # Single API call to get current state
            mapping = self._refresh_known_sheets()

            # Second check after refresh (handles race on startup)
            if sheet_name in mapping:
                return sheet_name

            # Try to copy from TEMPLATE
            if 'TEMPLATE' in mapping:
                ok = self._duplicate_sheet(mapping['TEMPLATE'], sheet_name)
                if ok:
                    logger.info(f"Created monthly sheet '{sheet_name}' from TEMPLATE")
                    return sheet_name

            # Fallback: add blank sheet and write header
            try:
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={'requests': [{'addSheet': {
                        'properties': {'title': sheet_name}
                    }}]}
                ).execute()
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption='USER_ENTERED',
                    body={'values': [self.HEADER_ROW]}
                ).execute()
                self._known_sheets.add(sheet_name)
                logger.info(f"Created monthly sheet '{sheet_name}' (blank)")
            except Exception as e:
                logger.error(f"Error creating sheet '{sheet_name}': {e}")

            return sheet_name

    def ensure_template_sheet(self):
        """
        Create a TEMPLATE tab with the correct header if it does not exist.
        """
        if not self.available:
            return
        with self._sheet_lock:
            mapping = self._refresh_known_sheets()
            if 'TEMPLATE' in mapping:
                return
            try:
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={'requests': [{'addSheet': {
                        'properties': {'title': 'TEMPLATE'}
                    }}]}
                ).execute()
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range="'TEMPLATE'!A1",
                    valueInputOption='USER_ENTERED',
                    body={'values': [self.HEADER_ROW]}
                ).execute()
                self._known_sheets.add('TEMPLATE')
                logger.info("Created TEMPLATE sheet")
            except Exception as e:
                logger.error(f"Error creating TEMPLATE: {e}")

    # ------------------------------------------------------------------
    # Data operations
    # ------------------------------------------------------------------

    def append_row(self, row: List[str], sheet_name: str = None) -> bool:
        if not self.available:
            return False
        if sheet_name is None:
            sheet_name = self.get_or_create_monthly_sheet()
        try:
            self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet_name}'!A:Z",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]}
            ).execute()

            return True
        
        except Exception as e:
            logger.error(f"Error appending row: {e}")
            return False

    def get_all_rows(self, sheet_range: str = None) -> List[List[str]]:
        if not self.available:
            return []
        if sheet_range is None:
            sheet_name = self.get_or_create_monthly_sheet()
            sheet_range = f"'{sheet_name}'!A:Z"
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=sheet_range
            ).execute()

            values = result.get("values", [])
            return values

        except requests.exceptions.ReadTimeout:
            logger.info("Google sheets read timeout, trying again")
            time.sleep(5)
            return self.get_all_rows(sheet_range)

        except Exception as e:
            logger.error(f"Error reading sheet: {e}")
            logger.info("Google sheets read timeout, trying again")
            time.sleep(5)
            return self.get_all_rows(sheet_range)

    def update_cell(self, row: int, col: int, value: str,
                    sheet_name: str = None) -> bool:

        if not self.available:
            return False
        if sheet_name is None:
            sheet_name = self.get_or_create_monthly_sheet()
        try:
            col_letter = chr(65 + col)
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{sheet_name}'!{col_letter}{row}",
                valueInputOption='USER_ENTERED',
                body={'values': [[value]]}
            ).execute()

            return True
        
        except Exception as e:
            logger.error(f"Error updating cell: {e}")
            return False

    def get_profit_loss(self, sheet_name: str = None) -> Dict:
        data = {}
        if sheet_name is None:
            sheet_name = self.get_or_create_monthly_sheet()

        result_C = self.service.spreadsheets().values() \
            .get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!B2:C7",
            valueRenderOption="UNFORMATTED_VALUE"
            ).execute()
    
        values_C = result_C.get("values", [])
        result_D = self.service.spreadsheets().values() \
                .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet_name}!D2:E7",
                valueRenderOption="UNFORMATTED_VALUE"
                ).execute()
        
        values_D = result_D.get("values", [])

        for row in values_C:
            desc, value = row[0], row[1]
            data[desc] = value       

        for row in values_D:
            desc, value = row[0], row[1]
            data[desc] = value       

        return data
    
    def get_overview(self) -> float:
        result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range='Dashboard!A14:C20',
                valueRenderOption="UNFORMATTED_VALUE"

            ).execute()
        
        data = {}
        for row in result.get('values', []):
            desc, value = row[0], row[2]
            data[desc] = value

        return data

    def get_bankroll(self) -> float:
        if not self.available:
            return 500
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range='Dashboard!A14:C20',
                valueRenderOption="UNFORMATTED_VALUE"

            ).execute()

            for row in result.get('values', []):
                if len(row) >= 2 and row[0].lower().strip() == 'bankroll':
                    bankroll = float(row[2])
                    return bankroll
        except Exception:
            pass

        return 500

    def update_settlement(self, fixture_id: str, settlement: str, outcome_id: str,
                        sheet_name: str = None) -> bool:

        rows = self.get_all_rows(sheet_name)
        header_row = 10  # rij 11 in Sheets

        if rows:
            if len(rows) <= header_row:
                return False

            headers = [
                h.lower() if h else ''
                for h in rows[header_row]
            ]
            settlement_col = None

            for j, h in enumerate(headers):
                if 'settlement' in h:
                    settlement_col = j
                    break

            if settlement_col is None:
                logger.info("No settlement column found in sheet")
                return False
            
            
            for i, row in enumerate(rows[header_row + 1:], start=header_row + 1):
                # fixture ID staat in kolom C
                if len(row) > 2 and row[3] == fixture_id \
                    and row[4] == outcome_id:

                    # i is de echte index in rows
                    return self.update_cell(
                        i + 1,
                        settlement_col,
                        settlement,
                        sheet_name
                    )

        return False

# ---------------------------------------------------------------------------
# Manual bet entry state machine
# ---------------------------------------------------------------------------

MANUAL_STEPS = [
    ('match',        'Wedstrijd (bijv. Arsenal - Chelsea):'),
    ('start_time',   'Starttijd (bijv. 2026-07-15 21:00):'),
    ('league',       'Competitie (bijv. Premier League):'),
    ('category',     'Land (bijv. England):'),
    ('sport',        'Soort sport (bijv. Football):'),
    ('market',       'Markt (bijv. 1X2):'),
    ('outcome',      'Uitkomst (bijv. Home / Draw / Away):'),
    ('soft_book',    'Bookmaker (bijv. cashpoint):'),
    ('soft_odds',    'Odds bij bookmaker (bijv. 2.15):'),
    ('sharp_odds',   'Sharp referentie win kans (bijv. 40%)):'),
    #('betslip',     'Betslip of - indien niet beschikbaar):')
]


class ManualBetSession:
    """Tracks the state of an active manual-entry conversation for one chat."""

    def __init__(self, min_ev_threshold: float = 2.0, kelly_fraction: float = 0.25):
        self.step_index = 0
        self.data: Dict[str, str] = {}
        self.min_ev_threshold = min_ev_threshold
        self.kelly_fraction = kelly_fraction
       
    @property
    def current_step(self) -> Optional[Tuple[str, str]]:
        if self.step_index < len(MANUAL_STEPS):
            return MANUAL_STEPS[self.step_index]
        return None
    
    
    def record_answer(self, answer: str):
        key, _ = MANUAL_STEPS[self.step_index]
        self.data[key] = answer.strip()
        self.step_index += 1

    @property
    def is_complete(self) -> bool:
        return self.step_index >= len(MANUAL_STEPS)
    

    def calculate_kelly(self, probability: float, odds: float) -> float:
        if odds <= 1:
            return 0
        b = odds - 1
        q = 1 - probability
        kelly = (probability * b - q) / b
        return max(0, kelly)
    
    def calculate_stake(self, probability: float, odds: float, bankroll: float,
                        fraction: float = 0.25) -> Tuple[float, float]:
        full_kelly = self.calculate_kelly(probability, odds)
        fractional_kelly = full_kelly * fraction
        stake_amount = bankroll * fractional_kelly
        return stake_amount, fractional_kelly

    def to_value_bet(self, bankroll: float) -> 'ValueBet':
        d = self.data
        parts = d['match'].split('-', 1)
        p1 = parts[0].strip()
        p2 = parts[1].strip() if len(parts) > 1 else ''
        soft_odds = float(d['soft_odds'])
        sharp_odds = float(d['sharp_odds'])
        win_prob = sharp_odds / 100 if sharp_odds > 0 else 0
        sport = d['sport']

        ev = ((win_prob * soft_odds) - 1) * 100
        if ev >= self.min_ev_threshold:
            stake, kelly_pct = self.calculate_stake(
                win_prob, soft_odds, bankroll, self.kelly_fraction)

            
            #betslip = d['betslip'] if d['betslip'] != '-' else None
            return ValueBet(
                fixture_id=f"manual_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                participant1=p1,
                participant2=p2,
                start_time=d['start_time'],
                tournament_name=d['league'],
                category_name=d['category'],
                market=d['market'],
                market_id='manual',
                outcome=d['outcome'],
                outcome_id='manual',
                sharp_bookmaker='manueel',
                sharp_odds=sharp_odds,
                soft_bookmaker=d['soft_book'],
                soft_odds=soft_odds,
                sport=sport,
                soft_bookmaker_odds={d['soft_book']: soft_odds},
                ev_percentage=ev,
                win_probability=win_prob,
                stake_amount=stake,
                bankroll=bankroll,
                kelly_fraction=kelly_pct,
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                betslip_url=None, #betslip
                settlement_status='PENDING',
                possible_profit= soft_odds * stake - stake
            )
        
        else:
            return None


class TelegramBot:
    """Telegram bot with commands, notifications, and manual bet entry"""

    def __init__(self, config: dict,
                 sheets: GoogleSheetsManager = None):

        self.bot_token = config['telegram_bot_token']
        self.chat_id = config['telegram_chat_id']
        self.chat_id_performance = config["performance_chat_id"]
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.sheets = sheets
        self.pending_bets: Dict[int, ValueBet] = {}
        self.last_update_id = 0
        self._scanner = None
        self.config = config
    
        # Per-chat manual entry sessions
        self._manual_sessions: Dict[str, ManualBetSession] = {}

    def set_scanner(self, scanner):
        self._scanner = scanner

    # ------------------------------------------------------------------
    # Messaging helpers
    # ------------------------------------------------------------------

    def send_message(self, text: str, chat_id: str = None,
                     keyboard: Dict = None) -> Optional[int]:

        cid = chat_id or self.chat_id
        payload: Dict = {
            "chat_id": cid,
            "text": text,
            "parse_mode": "Markdown"
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage", json=payload
            )
            result = response.json()
            if result.get('ok'):
                return result['result']['message_id']
        except Exception as e:
            logger.error(f"Error sending message: {e}")
        return None

    def edit_message(self, message_id: int, text: str, chat_id: str = None):
        cid = chat_id or self.chat_id
        try:
            requests.post(
                f"{self.base_url}/editMessageText",
                json={
                    "chat_id": cid,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": {"inline_keyboard": []}
                }
            )
        except Exception:
            pass

    def answer_callback(self, callback_id: str, text: str = ""):
        try:
            requests.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text}
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Value bet notification
    # ------------------------------------------------------------------

    def _format_odds_table(self, soft_odds: Dict[str, float],
                           best_book: str, sharp_odds: float) -> str:
        """
        Build a compact Markdown odds overview.
        Example:
          cashpoint   2.15 *
          unibet      2.10
          betano      2.05
          ─────────────────
          Sharp ref   2.00 (mediaan)
        """
        lines = []
        for bk, o in sorted(soft_odds.items(), key=lambda x: -x[1]):
            marker = ' ✓' if bk == best_book else ''
            lines.append(f"  `{bk:<12}` {o:.2f}{marker}")
        lines.append(f"  `{'────────────':12}`")
        lines.append(f"  `{'Sharp ref':<12}` {sharp_odds:.3f} (mediaan)")
        return '\n'.join(lines)

    def send_value_bet_notification(self, bet: ValueBet) -> bool:
        odds_table = self._format_odds_table(
            bet.soft_bookmaker_odds, bet.soft_bookmaker, bet.sharp_odds
        )

        betslip_line = f"[Betslip]({bet.betslip_url})" if bet.betslip_url else "_geen betslip URL_"

        message = (
            f"*Value Bet Gevonden!*\n\n"
            f"*{bet.participant1}* vs *{bet.participant2}*\n\n"
            f"Start: {bet.start_time}\n"
            f"Competitie: {bet.tournament_name} ({bet.category_name})\n\n"
            f"Sport: {bet.sport}\n"
            f"Markt: {bet.market}\n"
            f"Uitkomst: *{bet.outcome}*\n\n"
            f"*Odds overzicht:*\n"
            f"{odds_table}\n\n"
            f"*EV: {bet.ev_percentage:.2f}%*\n"
            f"Win kans: {bet.win_probability:.1%}\n\n"
            f"*Inzet: €{bet.stake_amount:.2f}*\n"
            f"Mogelijke winst: €{bet.possible_profit:.2f}\n"
            f"(Kelly: {bet.kelly_fraction:.2%} van {bet.bankroll:.0f})\n\n"
            f"{betslip_line}"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": "Bevestigen", "callback_data": f"confirm_{bet.fixture_id}_{bet.soft_bookmaker}_{bet.outcome_id}"},
                {"text": "Afwijzen",   "callback_data": f"reject_{bet.fixture_id}"}
            ]]
        }

        msg_id = self.send_message(message, keyboard=keyboard)
        if msg_id is not None:
            self.pending_bets[msg_id] = bet
            return True
        return False

    # ------------------------------------------------------------------
    # Update polling
    # ------------------------------------------------------------------
    
    def get_updates(self, timeout: int = 5) -> List[Dict]:
        try:
            response = requests.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self.last_update_id + 1,
                    "timeout": timeout
                },
                timeout=(10, timeout + 10)   # connect timeout, read timeout
            )

            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                logger.warning(f"Telegram API returned: {result}")
                return []

            updates = result.get("result", [])

            if updates:
                self.last_update_id = updates[-1]["update_id"]

            return updates

        except requests.exceptions.ConnectTimeout:
            logger.warning("Telegram connect timeout")

        except requests.exceptions.ReadTimeout:
            logger.warning("Telegram read timeout")

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Telegram connection error: {e}")
            time.sleep(5)

        except requests.exceptions.HTTPError as e:
            logger.error(f"Telegram HTTP error: {e}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Telegram request error: {e}")

        except ValueError:
            logger.error("Telegram returned invalid JSON")

        except Exception:
            logger.exception("Unexpected error while getting Telegram updates")

        return []



    def process_update(self, update: Dict) -> Optional[Dict]:
        if 'callback_query' in update:
            return self._handle_callback(update['callback_query'])
        if 'message' in update:
            return self._handle_message(update['message'])
        return None

    # ------------------------------------------------------------------
    # Callbacks (confirm / reject)
    # ------------------------------------------------------------------

    def _handle_callback(self, callback: Dict) -> Optional[Dict]:
        callback_id = callback['id']
        data = callback.get('data', '')
        message_id = callback['message'].get('message_id')

        self.answer_callback(callback_id)

        if data.startswith('confirm_') and message_id in self.pending_bets:
            bet = self.pending_bets[message_id]
            return {'action': 'confirm', 'bet': bet, 'message_id': message_id}

        if data.startswith('reject_') and message_id in self.pending_bets:
            bet = self.pending_bets.pop(message_id)
            
            return {'action': 'reject', 'bet': bet, 'message_id': message_id}

        return None

    # ------------------------------------------------------------------
    # Message / command handling
    # ------------------------------------------------------------------

    def _handle_message(self, message: Dict) -> Optional[Dict]:
        text = message.get('text', '').strip()
        chat_id = str(message.get('chat', {}).get('id', self.chat_id))

        # If there is an active manual-entry session for this chat, feed the answer
        if chat_id in self._manual_sessions and not text.startswith('/'):
            return self._manual_step(chat_id, text)

        if not text.startswith('/'):
            return None

        cmd = text.split()[0].lower()

        dispatch = {
            '/run':      lambda: {'action': 'run'},
            '/stop':     lambda: {'action': 'stop'},
            '/overview': lambda: {'action': 'overview'},
            '/current': lambda: {'action': 'current'},
            '/show_config': self._cmd_show_config,
            '/set':      lambda: {'action': 'set'},
            '/manueel':  lambda: self._cmd_manueel(chat_id),
            '/annuleer': lambda: self._cmd_annuleer(chat_id),
            '/help':     self._cmd_help,
        }

        handler = dispatch.get(cmd)
        print(cmd)
      
        if handler:
            if cmd == '/run':
                self.send_message("*Scanner GESTART*", chat_id=chat_id)
            elif cmd == '/stop':
                self.send_message("*Scanner GESTOPT*", chat_id=chat_id)
            if cmd == '/set':
                self.send_message("*UPDATING Settlements*", chat_id=self.chat_id_performance)
           
            
            return handler()
        return None

    # ------------------------------------------------------------------
    # Manual bet entry flow
    # ------------------------------------------------------------------

    def _cmd_manueel(self, chat_id: str) -> Dict:
        session = ManualBetSession()
        self._manual_sessions[chat_id] = session
        _, question = session.current_step
        self.send_message(
            f"*Manuele bet invoer*\n\nStap 1/{len(MANUAL_STEPS)}: {question}\n\n"
            f"_(Typ /annuleer om te stoppen)_",
            chat_id=chat_id
        )
        return {'action': 'manueel_start'}

    def _cmd_annuleer(self, chat_id: str) -> Dict:
        self._manual_sessions.pop(chat_id, None)
        self.send_message("*Invoer geannuleerd.*", chat_id=chat_id)
        return {'action': 'manueel_cancel'}

    def _manual_step(self, chat_id: str, answer: str) -> Optional[Dict]:
        session = self._manual_sessions[chat_id]
        session.record_answer(answer)

        if not session.is_complete:
            step_num = session.step_index + 1
            _, question = session.current_step
            self.send_message(
                f"Stap {step_num}/{len(MANUAL_STEPS)}: {question}",
                chat_id=chat_id
            )
            return {'action': 'manueel_step'}

        # All answers collected — build the bet
        bankroll = self.sheets.get_bankroll() if self.sheets else 500
        try:
            bet = session.to_value_bet(bankroll)

        except (ValueError, KeyError) as e:
            self.send_message(
                f"*Fout bij verwerking:* {e}\n\nStart opnieuw met /manueel",
                chat_id=chat_id
            )
            del self._manual_sessions[chat_id]
            return {'action': 'manueel_error'}

        del self._manual_sessions[chat_id]

        if bet is None:
            self.send_message("Bet voldoet niet aan de criteria, log een ander bet")

        # Show summary with confirm/reject buttons
        odds_table = self._format_odds_table(
            bet.soft_bookmaker_odds, bet.soft_bookmaker, bet.sharp_odds
        )
        summary = (
            f"*Samenvatting manuele bet*\n\n"
            f"*{bet.participant1}* vs *{bet.participant2}*\n"
            f"Start: {bet.start_time}\n"
            f"Sport: {bet.sport}\n"
            f"Competitie: {bet.tournament_name} ({bet.category_name})\n\n"
            f"Markt: {bet.market} | Uitkomst: *{bet.outcome}*\n\n"
            f"*Odds overzicht:*\n{odds_table}\n\n"
            f"*EV: {bet.ev_percentage:.2f}%*\n"
            f"Win kans: {bet.win_probability:.1%}\n\n"
            f"*Inzet: €{bet.stake_amount:.2f}*\n"
            f"Mogelijke winst: €{bet.stake_amount * bet.soft_odds - bet.stake_amount:.2f}\n"
            f"(Kelly: {bet.kelly_fraction:.2%} van {bet.bankroll:.0f})\n\n"
            f"Bet opslaan?"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "Opslaan", "callback_data": f"confirm_{bet.fixture_id}_{bet.soft_bookmaker}_manual"},
                {"text": "Annuleer", "callback_data": f"reject_{bet.fixture_id}"}
            ]]
        }
        msg_id = self.send_message(summary, chat_id=chat_id, keyboard=keyboard)
        if msg_id is not None:
            self.pending_bets[msg_id] = bet

        return {'action': 'manueel_complete', 'bet': bet}

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------
    def _cmd_show_config(self) -> Dict:

        sports = "\n".join(
            next(iter(mapping[str(id)]))
            for id in self.config.get("sport_id", [])
            )

        bookies = "\n".join(bookie for bookie in OddsPapiClient.SOFT_BOOKMAKERS)

        msg = f"""*Settings*\n\n
Min ev threshold: {self.config.get('min_ev_threshold', "Onbekend")}
Min win chance: {self.config.get('min_win_probability', "Onbekend")}
Kelly f: {self.config.get('kelly_fraction', "Onbekend")}
Max tournaments: {self.config.get('max_tournaments', "Onbekend")}
Days ahead: {self.config.get('days_ahead', "Onbekend")}\n
*Sports*:\n{sports}\n
*Bookies*:
{bookies}
        """

        self.send_message(msg, chat_id=self.chat_id_performance)
        return {'action': 'config'}


    def _cmd_profit(self) -> Dict:
        if self.sheets:
            p = self.sheets.get_profit_loss()
            total_bets = float(p['Totaal Bets'])
            open_bets = float(p['Open Bets'])
            bets_won = float(p['Gewonnen Bets'])
            bets_lost = float(p["Verloren Bets"])
            inzet = float(p['Inzet'])
            win_rate = float(p['Winrate'])
            roi = float(p['ROI'])
            ev = float(p['Gemiddelde EV'])
            profit = float(p['Winst'])

            msg = f"""*Maand Overview*\n\n
Totaal Bets: {total_bets}
Open Bets: {open_bets}
Gewonnen Bets: {bets_won}
Verloren Bets: {bets_lost}
Win Rate: {win_rate:.1%}
Gemiddelde EV: {ev:.1%}\n
Inzet: €{inzet:.2f}
Winst: €{profit:.2f}
            
            """
            
            return msg

        else:
            return "Google Sheets niet geconfigureerd"

    def _cmd_overview(self) -> Dict:
        if self.sheets:
            br = self.sheets.get_bankroll()
            data = self.sheets.get_overview()
            avg_roi = data.get('Average ROI', 0)
            avg_roi = float(avg_roi) if isinstance(avg_roi, float) else 0
            avg_ev = float(data.get('Average EV', 0))
            avg_win = float(data.get('Average win rate', 0))
            total_profit = float(data.get('Totaal winst', 0))

            msg = f"""*Totaal Overzicht*\n\n
Bankroll: €{br:.2f}
Average ROI: {avg_roi:.1%}
Average EV: {avg_ev:.1%}
Average winrate: {avg_win:.2%}
Profit: €{total_profit:.2f}

"""
            
            return msg

        else:
            return "Google Sheets niet geconfigureerd"
    

    def _cmd_help(self) -> Dict:
        self.send_message(
            "*Beschikbare commando's*\n\n"
            "/run - Scanner starten\n"
            "/stop - Scanner stoppen\n"
            "/manueel - Bet handmatig invoeren\n"
            "/annuleer - Manuele invoer annuleren\n"
            "/current - Winst/verlies overzicht\n"
            "/overiew - Toon totaaloverzicht\n"
            "/set - Settlements bijwerken\n"
            "/help - Dit overzicht"
        )
        return {'action': 'help'}


class ValueBetScanner:
    """Main scanner orchestrator"""

    def __init__(self, config: Dict):
        self.config = config
        self.is_scanning = False
        self.settlements = []
        self._market_mapping = []

        api_keys = config.get('oddspapi_keys', [])
        if not api_keys:
            single = config.get('oddspapi_key', '')
            api_keys = [single] if single else []


        if not api_keys or not api_keys[0]:
            raise ValueError("No API keys configured")


        self.odds_client = OddsPapiClient(
            api_keys, self.settlements, config.get('requests_per_key', 250), 
        )
        logger.info(f"Initialized with {len(api_keys)} API key(s)")

        self.calculator = ValueBetCalculator(
            min_ev_threshold=config.get('min_ev_threshold', 2.0),
            kelly_fraction=config.get('kelly_fraction', 0.25),
            min_win_prob=config.get('min_win_probability')
        )
        self.calculator.set_odds_client(self.odds_client)

        ManualBetSession(min_ev_threshold=config.get('min_ev_thresold', 2.0),
                         kelly_fraction=config.get('kelly_fraction', 0.25))

        self.sheets = None
        if config.get('google_credentials_path') and config.get('google_spreadsheet_id'):
            self.sheets = GoogleSheetsManager(
                config['google_credentials_path'],
                config['google_spreadsheet_id']
                
            )
            self.sheets.ensure_template_sheet()

        self.telegram = None
        if config.get('telegram_bot_token') and config.get('telegram_chat_id'):
            self.telegram = TelegramBot(
                config,
                self.sheets
            )
            self.telegram.set_scanner(self)

        self.seen_bets: set = set()
        self.confirmed_bets: List[Dict] = []
 
        self._load_seen()

    def _load_seen(self):
        try:
            if Path('seen_bets.json').exists():
                self.seen_bets = set(json.load(open('seen_bets.json')))
            if Path('confirmed_bets.json').exists():
                with open('confirmed_bets.json') as f:
                    self.confirmed_bets = json.load(f)

                self.confirmed_bet_keys = {
                    f"{b['fixture_id']}_{b['outcome_id']}"
                    for b in self.confirmed_bets
                
                }
            
            
        except Exception:
            pass

    def _save_seen(self):
        json.dump(list(self.seen_bets), open('seen_bets.json', 'w'))

    def _save_confirmed(self, bet: ValueBet):
        data = {
            'fixture_id': bet.fixture_id,
            'market_id': bet.market_id,
            'outcome_id': bet.outcome_id,
            'timestamp': bet.timestamp,
            'soft_bookmaker': bet.soft_bookmaker,
            'soft_odds': bet.soft_odds,
            'possible_profit': bet.possible_profit,
            'stake_amount': bet.stake_amount,
            'status': 'open'
        }

        self.confirmed_bets.append(data)
        with open('confirmed_bets.json', 'r') as f:
            bets = json.load(f)
        
        bets.append(data)

        with open('confirmed_bets.json', 'w') as f:
            json.dump(bets, f, indent=2)
        

    def get_bankroll(self) -> float:
        if self.sheets:
            return self.sheets.get_bankroll()
        return float(self.config.get('bankroll', 500))


    def settle_match_winner(self, outcome_id, result):
        try:
            goals_ht = sum([result['home_ht'], result['away_ht']])
            goals_ft = sum([result['home_score'], result['away_score']])

        except:
            pass

        print(outcome_id, result)
        if outcome_id in ['181']:
            return result['away_end'] < result['home_end']

        if outcome_id in ['12246']:
            return result['home_end'] == 0

        if outcome_id in ['12245']:
            return result['home_end'] > 0

        if outcome_id in ['12247']:
            return result['away_end'] > 0

        if outcome_id in ['12248']:
            return result['away_end'] == 0

        if outcome_id in ['121033']:
            return result['home_end'] > 0 and result['away_end'] > 0

        if outcome_id in ['121']:
            return result['home_end'] > result['away_end']

        if outcome_id in ['122']:
            return result['home_end'] < result['away_end']

        if outcome_id in ['123']:
            return result['home_ht'] > result['away_ht']

        if outcome_id in ['124']:
            return result['home_ht'] < result['away_ht']

        if outcome_id in ['125']:
            return result['home_st'] > result['away_st']

        if outcome_id in ['126']:
            return result['away_st'] > result['home_st']
        
        if outcome_id in ['182']:
            return result['away_end'] > result['home_end']

        if outcome_id in ['181']:
            return result['home_end'] > result['away_end']
        
        if outcome_id in ['111', '141', '191', '131', '313', '311']:
            return result['home_score'] > result['away_score']

        if outcome_id == '101':
            return result['home_end'] > result['away_end']

        if outcome_id == '102':
            return result['home_end'] == result['away_end']

        if outcome_id == '103':
            return result['home_end'] < result['away_end']
        
        if outcome_id in ['314']:
            return result['home_score'] == result['away_score']

        if outcome_id in ['112', '142', '192', '132', '182', '315', '312']:
            return result['away_score'] > result['home_score']

        if outcome_id == '104':
            return result['home_score'] != 0 and result['away_score'] != 0

        if outcome_id == '105':
            return (result['home_score'] > 0 and result['away_score'] == 0) \
            or (result['away_score'] > 0 and result['home_score'] == 0)

        if outcome_id == '10302':
            return goals_ht <= goals_ft

        if outcome_id == '10303':
            return (goals_ht != 0 and goals_ft > goals_ht)

        if outcome_id == '101902':
            return result['home_score'] > result['away_score'] \
                    or result['home_score'] == result['away_score']

        if outcome_id == '101903':
            return result['home_score'] > result['away_score'] \
                    or result['home_score'] < result['away_score']

        if outcome_id == '101904':
            return result['home_score'] == result['away_score'] or \
                    result['away_score'] > result['home_score']

        if outcome_id == '10208':
            if (result['home_ht'] and result['away_ht']) is None:
                return None
            
            return result['home_ht'] > result['away_ht']

        if outcome_id == '10209':
            return result['home_ht'] == result['away_ht']

        if outcome_id == '10210':
            return result['away_ht'] > result['home_ht']

        if outcome_id == '10211':
            return result['home_score'] > result['away_score']

        if outcome_id == '10212':
            return result['home_score'] == result['away_score']

        if outcome_id == '10213':
            return result['away_score'] > result['home_score']

        if outcome_id == '108':
            return goals_ft > 1.5

        if outcome_id == '109':
            return goals_ft < 1.5

        if outcome_id == '1010':
            return goals_ft > 2.5

        if outcome_id == '1011':
            return goals_ft < 2.5

        if outcome_id == '1012':
            return goals_ft > 3.5

        if outcome_id == '1013':
            return goals_ft < 3.5

        #if outcome_id == '193':
            #return 
        return None
    

    def update_settlements(self) -> str:
        if not self.confirmed_bets:
            return "Geen bets om bij te werken"

        fixture_ids = []
        for b in self.confirmed_bets:
            fixture_id = b['fixture_id']
            data = self.odds_client.get_fixture(fixture_id)
            if data:
                try:
                    status = data['statusName']
                    if not status == "Finished":
                        continue

                except KeyError:
                    continue

            if not b['fixture_id'].startswith('manual_') \
                    and b['status'] == 'open':

                    fixture_ids.append(fixture_id)

        scores = self.odds_client.get_scores(fixture_ids)
        updated = wins = losses = 0
        if not scores:
            logger.info("Cannot set settlements, no finished bets found")
            return "Geen beëindigde bets gevonden om bij te werken"

        if self.sheets:
            total_profit = None
            total_loss = None
            for i in scores:
                for bet in self.confirmed_bets:
                    fid = bet['fixture_id']
                    if fid != i.get('fixtureId', None):
                        continue

                    outcome_id = bet['outcome_id']
                    #possible_profit = bet.get('possible_profit', None)
                    #potential_loss = bet['stake_amount']

                    if (fid == i['fixtureId'] and bet['status'] == 'open'):
                        results = i.get('scores').get('periods') 
                            
                        half_time_result = results.get("p1", None)
                        full_time_result = results.get("fulltime", None)
                        end_score = results.get("result", None)
                        second_time_result = results.get("p2", None)

                        end_score_home = float(end_score.get("participant1Score"))
                        end_score_away = float(end_score.get("participant2Score"))

                        result = {
                            "home_score": None,
                            "away_score": None,
                            "home_ht": None,
                            "away_ht": None,
                            "home_st": None,
                            "away_st": None,
                            "home_end": end_score_home,
                            "away_end": end_score_away
                            }


                        if half_time_result:
                            result['home_ht'] = float(half_time_result.get("participant1Score"))
                            result['away_ht'] = float(half_time_result.get("participant2Score"))
                            
                        if full_time_result:
                            result['home_score'] = float(full_time_result.get("participant1Score"))
                            result['away_score'] = float(full_time_result.get("participant2Score"))
                           
                        if second_time_result:
                            result['home_st'] = float(second_time_result.get("participant1Score"))
                            result['away_st'] = float(second_time_result.get("participant2Score"))
                        

                        
                        win = self.settle_match_winner(outcome_id, result)
                        
                        status = None
                        if win is not None:
                            if win:
                                wins += 1
                                status = "WIN"
                                
                            elif not win:
                                losses += 1
                                status = "LOSE"

                            print("-----------------------------------------")
                            print("MATCH", fid, outcome_id, status)
                            print(result)
                            
                            succes = self.sheets.update_settlement(fid, status, outcome_id)
                            if succes:
                                print("Settlement Updated!")
                                print("-----------------------------------------")
                                if (
                                    bet['fixture_id'] == fid
                                    and bet['outcome_id'] == outcome_id
                                    ):
                                    bet['status'] = 'closed'
                            
                                with open('confirmed_bets.json', 'w') as f:
                                    json.dump(self.confirmed_bets, f, indent=2)

                                updated += 1

            #profit = round(total_profit - total_loss, 2)
            msg_current = self.telegram._cmd_profit()

            return (
                f"Bijgewerkt: {updated}\n"
                f"Gewonnen: {wins}\n"
                f"Verloren: {losses}\n\n"
                #+ (f"€{profit} " + ("Winst" if profit > 0 else "Verlies") if possible_profit is not None else "")
                f"{msg_current}")
                

    def update_main_sheet_totals(self):
        """Sum A2:F6 from all MM-YYYY sheets."""
        return self.sheets.update_main_sheet_totals()


    def scan_once(self) -> List[ValueBet]:
        logger.info("Scanning...")
        bankroll = self.get_bankroll()

        sport_ids = self.config.get('sport_id', [])
    
        if not sport_ids:
            raise ValueError("No sport id's configured")

        counter = 0
        for id in sport_ids:
            value_bets = []
            tournaments = self.odds_client.get_tournaments(id)
            if tournaments is None:
                logger.info("Stopping scanner due to unforseen problems")
                msg = "Kon data niet ophalen, probeer opnieuw met andere keys of roteer IP adress"
                self.is_scanning = False
                self.telegram.send_message(msg)
                return 

            active = [t for t in tournaments
                    if t.get('upcomingFixtures', 0) > 0 or t.get('futureFixtures', 0) > 0]
  
            for tournament in active[:self.config.get('max_tournaments', 10)]:
                if not self.is_scanning:
                    break
                
                fixtures = self.odds_client.get_fixtures(
                    tournament_id=tournament['tournamentId'],
                    sport_id=id,
                    days_ahead=self.config.get('days_ahead', 7)
                )

          
                if fixtures is None:
                    msg = "Kon data niet ophalen, probeer opnieuw met andere keys of roteer IP adress"
                    logger.info("Stopping scanner due to unforseen problems")
                    self.is_scanning = False
                    self.telegram.send_message(msg)
                    return 

                for fixture in fixtures:
                    if not self.is_scanning:
                        break

                    odds_data = self.odds_client.get_odds(fixture['fixtureId'])
                    if odds_data is None:
                        logger.info("Stopping scanner due to unforseen problems")
                        msg = "Kon data niet ophalen, probeer opnieuw met andere keys of roteer IP adress"
                        self.is_scanning = False
                        self.telegram.send_message(msg)
                        return 

                    if not odds_data.get('bookmakerOdds'):
                        continue

                    bets = self.calculator.analyze_fixture(fixture, odds_data, bankroll)
                    for bet in bets:
                        key = f"{bet.fixture_id}_{bet.outcome_id}"
                        
                        if key not in self.confirmed_bet_keys:
                            value_bets.append(bet)

                    time.sleep(self.config.get('request_delay', 1))


            counter += len(value_bets)
            finished_msg = f"{counter} value bets gevonden"
            logger.info(f"Found {len(value_bets)} value bets for sport ID {id}")
            self.is_scanning = True

            value_bets.sort(
                key=lambda bet: bet.ev_percentage,
                reverse=True
            )

            for bet in value_bets:
                if not self.is_scanning:
                    break

                if self.telegram:
                    self.telegram.send_value_bet_notification(bet)


        self.telegram.send_message("Scanner *KLAAR*") 
        self.telegram.send_message(finished_msg)       

         
    def run_interactive(self):
        if not self.telegram:
            logger.error("Telegram not configured")
            return

        self.telegram.send_message("""*Value Bet Scanner Gestart*\n
Gebruik /run om de scanner te starten
Gebruik /manueel om zelf een weddenschap te loggen.
/current om huidige prestaties te bekijken.
/help voor alle commando's"""
                                   
)
        scan_thread = None
        while True:
            try:
                for update in self.telegram.get_updates():
                    result = self.telegram.process_update(update)
         
                    if result:
                        try:
                            action = result.get('action') 
                        except:
                            action = None
                        
                        if action == 'run':
                            print("RUNNING")
                            if not self.is_scanning:
                                self.is_scanning = True

                                scan_thread = threading.Thread(
                                    target=self.scan_once, daemon=True
                                )
                                scan_thread.start()

                        elif action == 'stop':
                            self.is_scanning = False

                        elif action == 'reject':
                            bet = result.get('bet')
                            message_id = result.get('message_id')
                            if bet and message_id:
                                self.telegram.edit_message(
                                message_id,
                                f"*AFGEWEZEN* ❌\n\n{bet.participant1} vs {bet.participant2}"
                            )
                            
                        elif action == 'confirm':
                            bet = result.get('bet')
                            message_id = result.get('message_id')

                            if bet and message_id:
                                success = self._log_bet(bet)

                                if success:
                                    # Pas verwijderen nadat het opslaan gelukt is
                                    self.telegram.pending_bets.pop(message_id, None)

                                    self.telegram.edit_message(
                                        message_id,
                                        f"*BEVESTIGD* ✅\n\n"
                                        f"{bet.participant1} vs {bet.participant2}\n"
                                        f"{bet.soft_bookmaker} @ {bet.soft_odds}\n"
                                        f"Inzet: {bet.stake_amount:.2f}"
                                    )

                                else:
                                    self.telegram.send_message(
                                        message_id,
                                        f"*LOGGEN MISLUKT* ❌\n\n"
                                        f"{bet.participant1} vs {bet.participant2}\n"
                                        f"{bet.outcome}\n\n"
                                        f"Probeer bet opnieuw te loggen."
                                    )
            
                                    
                        elif action == 'set':
                            logger.info("UPDATING settlements")
                            msg = self.update_settlements()
                            logger.info("Updating dashboard totals")
                            self.update_main_sheet_totals()
                            
                            self.telegram.send_message(f"*Settlements*\n\n"
                                                       f"{msg}\n",
                                                       self.telegram.chat_id_performance
                                                       )

                        elif action == 'current':
                            msg = self.telegram._cmd_profit()
                            self.telegram.send_message(msg, 
                                                       self.telegram.chat_id_performance)
                            
                        elif action == 'overview':
                            msg = self.telegram._cmd_overview()
                            self.telegram.send_message(msg,
                                                       self.telegram.chat_id_performance)
                

                time.sleep(1)

            except KeyboardInterrupt:
                self.is_scanning = False
                break
            except Exception:
                logger.exception(f"Loop error")
                time.sleep(5)

    def _log_bet(self, bet: ValueBet):
        """Write a confirmed bet to the monthly Google Sheet."""
        if self.sheets:
            d = bet.to_dict()
            start_date = d.get('Start wedstrijd').split(" ")[0]
            data = start_date.split('-')

            row = [d.get(h, '') for h in SHEET_HEADERS]
            sheet_name = self.sheets.get_or_create_monthly_sheet(year=data[0], month=data[1])
            if self.sheets.append_row(row, sheet_name=sheet_name):
                self._save_confirmed(bet)
                logger.info(f"Bet opgeslagen: {bet.fixture_id}")
                return True
            
            else:
                logger.info(f"Bet niet kunnen opslaan: {bet.fixture_id}")
                return False


    def run_single(self):
        bets = self.scan_once()
        logger.info(f"\n{'='*50}\nGEVONDEN {len(bets)} VALUE BETS\n{'='*50}")
        for bet in bets:
            logger.info(
                f"\n{bet.participant1} vs {bet.participant2}\n"
                f"{bet.soft_bookmaker} @ {bet.soft_odds}\n"
                f"EV: {bet.ev_percentage:.2f}%\n"
                f"Inzet: {bet.stake_amount:.2f}\n"
                f"Betslip: {bet.betslip_url}"
            )
            self._log_bet(bet)


def load_config(path: str = 'config.json') -> Dict:
    config = {}

    if Path(path).exists():
        config = json.load(open(path))
    else:
        logger.warning(f"No configuration file {path} found")
        return None
    
    return config


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Value Bet Scanner')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--interactive', action='store_true',
                        help='Telegram interactive mode')
    parser.add_argument('--sport', type=int, nargs='+', help="Sport ID's")
    parser.add_argument('--ev', type=float, help='Minimum EV % per bet')
    parser.add_argument('--days', type=float, help='Aantal dagen voorgaand aan de matches')
    parser.add_argument('--max_tournaments', type=float, help="Maximum aantal te verwerken tournaments")
    parser.add_argument('--kelly', type=float, help='Fractie van kelly om stake te bepalen, default: 0.1')
    parser.add_argument('--win_prob', type=float, help='Minimum % winkans op de outcome, default: 8')
    parser.add_argument('--set', help='Settlements bijwerken', action='store_true')
    parser.add_argument('--show_month_performance', help='Toon het overzicht van de huidige maand', action='store_true')
    parser.add_argument('--show_total_performance', help='Toon het totaaloverzicht', action='store_true')

    args = parser.parse_args()
    config = load_config(args.config)
    if config is None:
        return

    if args.sport is not None:
        config["sport_id"] = [args.sport]
      
    if args.ev is not None:
        config['min_ev_threshold'] = args.ev
    
    if args.days is not None:
        config['days_ahead'] = args.days
    
    if args.kelly is not None:
        config['kelly_fraction'] = args.kelly

    if args.max_tournaments is not None:
        config['max_tournaments'] = args.max_tournaments


    if not config.get('oddspapi_keys'):
        logger.error("API keys vereist")
        return


    scanner = ValueBetScanner(config)
    if args.set:
        logger.info("UPDATING settlements")
        msg = scanner.update_settlements()
        logger.info("Updating dashboard totals")
        scanner.telegram.send_message("Updating dashboard totals", scanner.telegram.chat_id_performance)
        msg_dashboard = scanner.update_main_sheet_totals()
        scanner.telegram.send_message(f"*Settlements*\n\n{msg}", scanner.telegram.chat_id_performance)
        scanner.telegram.send_message(msg_dashboard, scanner.telegram.chat_id_performance)


    if args.show_total_performance:
        msg = scanner.telegram._cmd_overview()
        scanner.telegram.send_message(msg, 
                                      scanner.telegram.chat_id_performance)

    else:
        if args.interactive:
            print("Running interactive")
            scanner.run_interactive()
 


if __name__ == '__main__':
    main()
