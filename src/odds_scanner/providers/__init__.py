from odds_scanner.providers.base import OddsProvider
from odds_scanner.providers.demo import DemoOddsProvider
from odds_scanner.providers.odds_api import OddsApiProvider
from odds_scanner.providers.oddspapi import OddsPapiProvider

__all__ = ["DemoOddsProvider", "OddsApiProvider", "OddsPapiProvider", "OddsProvider"]
