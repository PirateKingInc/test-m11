from betdata.sources.cfbd import CfbdSource
from betdata.sources.nflverse import NflverseSource
from betdata.sources.oddspapi import OddsPapiSource
from betdata.sources.sharpapi import SharpApiSource
from betdata.sources.the_odds_api import TheOddsApiSource

REGISTRY = {
    cls.name: cls
    for cls in (TheOddsApiSource, NflverseSource, CfbdSource, SharpApiSource, OddsPapiSource)
}
