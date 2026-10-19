from betdata.sources.cfbd import CfbdSource
from betdata.sources.nba_cdn import NbaCdnSource
from betdata.sources.nfl_model import NflModelSource
from betdata.sources.nflverse import NflverseSource
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
        NflModelSource,
        CfbdSource,
        SharpApiSource,
        OddsPapiSource,
        NbaCdnSource,
        SportsDataverseSource,
        OpenMeteoSource,
    )
}
