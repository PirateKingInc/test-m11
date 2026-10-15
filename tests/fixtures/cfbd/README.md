Hand-built responses in the shape of CFBD `GET /teams` (football, cfbd 5.x `Team` model)
and CollegeBasketballData `GET /teams` (cbbd 1.x `TeamInfo` model); no key was available.
They concentrate the hard cases: Miami vs Miami (OH) both claiming "Miami", USC vs South
Carolina, accents (San José State), apostrophes (Hawai'i, St. John's), an FCS team kept and a
D-II team dropped. Replace with recorded responses once CFBD_API_KEY is set.

`games_*` / `lines_*`: hand-built in the shape of cfbd 5.x `Game`/`BettingGame` and cbbd 1.x
`GameInfo`/`GameLines`, using the team ids above. Cases: FCS opponent kept, D-II opponent
skipped, neutral site, a provider whose raw `spread` is inverted relative to its
`formattedSpread` (Bovada, USC vs South Carolina), a postponed basketball game, a non-D-I
basketball opponent, and a basketball provider whose spread and moneylines disagree.
