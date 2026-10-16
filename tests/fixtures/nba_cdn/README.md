Hand-built in the shapes nba_api 1.11 defines for `stats.endpoints.ScheduleLeagueV2`
(`leagueSchedule.gameDates[].games[]`) and `live.nba.endpoints.ScoreBoard`; cdn.nba.com was not
reachable when these were written. Cases: a final preseason game, an upcoming game, a
postponed game ("PPD"), a neutral-site game, an All-Star game (gameId 003..., skipped), and a
scoreboard final for the upcoming game.
