from betdata.sources.cfbd import CfbdSource
from betdata.sources.espn_nba import EspnNbaSource
from betdata.sources.espn_props import EspnPropsSource
from betdata.sources.nba_cdn import NbaCdnSource
from betdata.sources.nba_injury import NbaInjurySource
from betdata.sources.nfl_model import NflModelSource
from betdata.sources.nflverse import NflverseSource
from betdata.sources.nflverse_extra import NflverseExtraSource
from betdata.sources.oddspapi import OddsPapiSource
from betdata.sources.open_meteo import OpenMeteoSource
from betdata.sources.sharpapi import SharpApiSource
from betdata.sources.sportsdataverse import SportsDataverseSource
from betdata.sources.the_odds_api import TheOddsApiSource

REGISTRY = {
    cls.name: cls
    for cls in (
        TheOddsApiSource,
        NflverseSource,
        NflverseExtraSource,
        NflModelSource,
        CfbdSource,
        SharpApiSource,
        OddsPapiSource,
        NbaCdnSource,
        SportsDataverseSource,
        OpenMeteoSource,
        EspnNbaSource,
        NbaInjurySource,
        EspnPropsSource,
    )
}
