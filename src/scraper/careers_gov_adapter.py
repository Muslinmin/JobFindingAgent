import httpx, json

APP_ID  = "3OW7D8B4IZ"
API_KEY = "32fa71d8b0bc06be1e6395bf8c430107"
INDEX   = "job_index"

url = f"https://{APP_ID}-dsn.algolia.net/1/indexes/{INDEX}/query"
# headers = {
#     "X-Algolia-Application-Id": APP_ID,
#     "X-Algolia-API-Key": API_KEY,
# }

headers = {
    "X-Algolia-Application-Id": APP_ID,
    "X-Algolia-API-Key": API_KEY,
    "Referer": "https://jobs.careers.gov.sg/",
    "Origin": "https://jobs.careers.gov.sg",
}
body = {"query": "engineer", "hitsPerPage": 3}   # no attributesToRetrieve → ask for everything

r = httpx.post(url, headers=headers, json=body)
print("status:", r.status_code)
data = r.json()
print("nbHits:", data.get("nbHits"))
hits = data.get("hits", [])
for h in hits[:2]:
    print(json.dumps(h, indent=2)[:1500])
    print("---")