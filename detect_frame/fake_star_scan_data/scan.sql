WITH 
  low_activity_users AS (
    SELECT
      actor_login AS actor,
      toDate(MIN(created_at)) AS first_active,
      toDate(MAX(created_at)) AS last_active,
      COUNT(DISTINCT created_at) AS n_actions,
      COUNT(DISTINCT repo_name) AS n_repos
    FROM 
      github_events
    WHERE 
      created_at BETWEEN '2025-10-01' AND '2025-12-31'
    GROUP BY 
      actor_login
    HAVING 
      first_active = last_active
      AND n_actions <= 2
      AND n_repos <= 1
  ),
  stars AS (
    SELECT
      actor_login AS actor,
      repo_name,
      if(actor_login IN (SELECT actor FROM low_activity_users), 1, 0) AS low_activity
    FROM 
      github_events
    WHERE 
      created_at BETWEEN '2025-10-01' AND '2025-12-31'
      AND event_type = 'WatchEvent'
  )
SELECT
  repo_name,
  COUNT(DISTINCT actor) AS n_stars,
  length(low_activity_actors) AS low_activity_stars,
  arrayDistinct(
    arrayFilter(
      x -> notEmpty(x),
      groupArrayIf(actor, low_activity = 1)
    )
  ) AS low_activity_actors
FROM 
  stars
GROUP BY 
  repo_name
HAVING 
  length(low_activity_actors) >= 50
ORDER BY 
  length(low_activity_actors) DESC;