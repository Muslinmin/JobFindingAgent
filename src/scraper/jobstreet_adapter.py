import httpx
from bs4 import BeautifulSoup

JOB_URL = "https://sg.jobstreet.com/job/92632502"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-SG,en;q=0.9",
}
r = httpx.get(JOB_URL, headers=headers, follow_redirects=True, timeout=30)
print("STATUS:", r.status_code)
print("FINAL URL:", str(r.url))
print("LENGTH:", len(r.text))

soup = BeautifulSoup(r.text, "html.parser")

# Most likely hook for the full JD on the detail page:
for label in ["jobAdDetails", "jobDescription", "job-detail-description",
              "jobAdDetail"]:
    el = soup.find(attrs={"data-automation": label})
    if el:
        text = el.get_text(" ", strip=True)
        print(f"\nFOUND data-automation='{label}': {len(text)} chars")
        print("  preview:", text[:200])
        break
else:
    print("\nNo known JD hook found — dumping all data-automation labels on this page:")
    seen = set()
    for el in soup.find_all(attrs={"data-automation": True}):
        da = el["data-automation"]
        if da not in seen:
            seen.add(da)
            print(f"  {da!r}: {el.get_text(strip=True)[:60]!r}")