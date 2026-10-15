from betdata.sources.nflverse import NflverseSource
from betdata.sources.the_odds_api import TheOddsApiSource

REGISTRY = {cls.name: cls for cls in (TheOddsApiSource, NflverseSource)}
