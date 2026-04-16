"""
TRACE Report Scraper v4 — Northeastern BlueRA
- JS click for all ASP.NET pager buttons (bypasses ElementNotInteractable)
- Non-fatal total-page detection
- JSON-only output
- Resume from checkpoint
"""

import json
import time
import random
import re
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
)
from bs4 import BeautifulSoup, Tag
import requests

# ── CONFIG ──────────────────────────────────────────
LIST_URL = (
    "https://northeastern-bc.bluera.com/rpvlf.aspx"
    "?rid=694b0639-6919-433a-9b04-5aaba2ab962a&regl=en-US&haslang=true"
)
OUTPUT_DIR = Path(f"trace_scrape_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
MIN_DELAY = 0.6
MAX_DELAY = 1.5
MAX_RETRIES = 3
CHECKPOINT_EVERY = 25


# ── DRIVER & AUTH ───────────────────────────────────
def create_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")
    return webdriver.Chrome(options=options)


def authenticate(driver):
    driver.get(LIST_URL)
    if "login.microsoftonline.com" in driver.current_url:
        print("🔐 SAML login required. Complete auth in the browser window.")
        print("   (Waiting up to 180s for redirect back to BlueRA...)")
        WebDriverWait(driver, 180).until(
            lambda d: "bluera.com" in d.current_url
            and "rpvlf" in d.current_url.lower()
        )
        print("✅ Auth complete!")
    else:
        print("✅ Already authenticated.")


def build_requests_session(driver):
    sess = requests.Session()
    for c in driver.get_cookies():
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://northeastern-bc.bluera.com/",
        }
    )
    return sess


# ── PAGER CLICK HELPER ─────────────────────────────
def js_click(driver, element):
    """
    Force-click an element via JavaScript.
    Bypasses ElementNotInteractableException, ElementClickInterceptedException,
    and any other visibility/overlay issues.
    """
    driver.execute_script("arguments[0].click();", element)


def scroll_and_click(driver, element):
    """Scroll element into view, then JS click it."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'center'});",
        element,
    )
    time.sleep(0.3)
    js_click(driver, element)


# ── PAGINATION ──────────────────────────────────────
def get_current_page_number(driver):
    """
    Current page in the ASP.NET pager is a bare <span>N</span>
    while other page numbers are wrapped in <a> tags.
    """
    try:
        pager_td = driver.find_element(By.CSS_SELECTOR, "tr.EUPagerStyle td")
        # Collect all direct text-node-holding elements
        # Current page is the <span> that is NOT inside an <a>
        for sp in pager_td.find_elements(By.TAG_NAME, "span"):
            txt = sp.text.strip()
            if txt.isdigit():
                # Make sure this span isn't wrapped in an anchor
                parent = sp.find_element(By.XPATH, "..")
                if parent.tag_name != "a":
                    return int(txt)
    except (NoSuchElementException, ValueError):
        pass
    return 1


def wait_for_report_links(driver, timeout=15):
    """Wait for report links to appear after a postback."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "a[href*='SelectedIDforPrint']")
            )
        )
        return True
    except TimeoutException:
        # Broader — just wait for any table
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "tr.EUPagerStyle"))
            )
            return True
        except TimeoutException:
            return False


def click_next_page(driver):
    """
    Click the Next (> ) submit button inside EUPagerStyle.
    Uses JavaScript click to bypass ElementNotInteractable.
    Returns False when on last page (button is disabled).
    """
    try:
        next_btn = driver.find_element(
            By.CSS_SELECTOR, "tr.EUPagerStyle input[name*='btnNext']"
        )
    except NoSuchElementException:
        return False

    # Check disabled attribute — means we're on the last page
    disabled = next_btn.get_attribute("disabled")
    if disabled is not None:
        return False

    # JS click bypasses all visibility/interactability issues
    scroll_and_click(driver, next_btn)

    # Wait for postback to complete
    time.sleep(1)
    wait_for_report_links(driver, timeout=15)
    return True


def get_total_page_count(driver):
    """
    Try to jump to the last page to discover total pages.
    Non-fatal — returns None if it fails for any reason.
    """
    try:
        last_btn = driver.find_element(
            By.CSS_SELECTOR, "tr.EUPagerStyle input[name*='btnLast']"
        )
        # If disabled, there's only one page
        if last_btn.get_attribute("disabled") is not None:
            return 1

        # JS click to bypass interactability issues
        scroll_and_click(driver, last_btn)
        time.sleep(1)
        wait_for_report_links(driver, timeout=15)

        total = get_current_page_number(driver)

        # Return to page 1
        try:
            first_btn = driver.find_element(
                By.CSS_SELECTOR, "tr.EUPagerStyle input[name*='btnFirst']"
            )
            scroll_and_click(driver, first_btn)
            time.sleep(1)
            wait_for_report_links(driver, timeout=15)
        except NoSuchElementException:
            pass

        return total

    except (NoSuchElementException, ElementNotInteractableException, Exception) as e:
        # Non-fatal — just skip total-page detection
        print(f"   (couldn't get total pages: {type(e).__name__}, continuing anyway)")
        return None


# ── LIST CRAWLER ────────────────────────────────────
def collect_report_links(driver):
    all_links = []
    seen_urls = set()
    page = 1
    consecutive_empty = 0

    # Try to discover total pages (non-fatal)
    total_pages = get_total_page_count(driver)
    if total_pages:
        print(f"📊 Pager reports {total_pages} total list-pages")
    else:
        print("📊 Total pages unknown — will paginate until done")

    while True:
        label = f"{page}/{total_pages}" if total_pages else str(page)
        print(f"📋 List page {label} — ", end="", flush=True)

        # Wait for report links to be present
        if not wait_for_report_links(driver, timeout=20):
            # Maybe page layout is different — try broader wait
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "table"))
                )
            except TimeoutException:
                print("no items found, stopping.")
                break

        # Brief grace period for JS rendering
        time.sleep(0.8)

        # Collect all report links on current page
        new_count = 0
        for sel in [
            "a[href*='SelectedIDforPrint']",
            "a[href*='rpvf-eng']",
            "a[href*='rpvf']",
        ]:
            links = driver.find_elements(By.CSS_SELECTOR, sel)
            for link in links:
                try:
                    href = link.get_attribute("href")
                    if href and "SelectedIDforPrint" in href and href not in seen_urls:
                        seen_urls.add(href)
                        all_links.append(href)
                        new_count += 1
                except StaleElementReferenceException:
                    continue
            if new_count > 0:
                break  # first working selector is fine

        print(f"+{new_count} new (total: {len(all_links)})")

        if new_count == 0:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print("3 empty pages in a row — stopping.")
                break
        else:
            consecutive_empty = 0

        # Try next page
        if not click_next_page(driver):
            print("📭 Reached last page.")
            break

        page += 1
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    return all_links


# ── REPORT PARSER ───────────────────────────────────
def parse_report(html_content, url):
    soup = BeautifulSoup(html_content, "html.parser")
    data = {
        "url": url,
        "metadata": {},
        "ratings": [],
        "comments": [],
        "demographics": [],
        "individual_responses": [],
    }

    # ── metadata ────────────────────────────
    title_tag = soup.find("h2")
    if title_tag:
        title_text = title_tag.get_text(strip=True)
        data["metadata"]["full_title"] = title_text
        m = re.match(
            r"Student TRACE report for\s+([\w]+\d+-\d+)\s+(.+?)\s*\((.+?)\)",
            title_text,
        )
        if m:
            data["metadata"]["course_code"] = m.group(1)
            data["metadata"]["course_name"] = m.group(2).strip()
            data["metadata"]["instructor"] = m.group(3).strip()

    # Semester
    for dl in soup.find_all("dl", class_="cover-page-project-title"):
        dd = dl.find("dd")
        if dd:
            sp = dd.find("span")
            if sp:
                data["metadata"]["semester"] = sp.get_text(strip=True)
                break
    if "semester" not in data["metadata"]:
        for sp in soup.find_all("span"):
            if sp.get("id", "").endswith("ProjectTitle"):
                data["metadata"]["semester"] = sp.get_text(strip=True)
                break

    # Prepared by / Created date
    for sp in soup.find_all("span"):
        sid = sp.get("id", "")
        if sid.endswith("_lbCreator"):
            strong = sp.find("strong")
            if strong:
                data["metadata"]["prepared_by"] = strong.get_text(strip=True)
        if sid.endswith("_lbPublishDateInfo"):
            strong = sp.find("strong")
            if strong:
                data["metadata"]["created_date"] = strong.get_text(strip=True)

    # Audience
    aud = soup.find("div", class_="audience-data")
    if aud:
        for item in aud.find_all("div", class_="audience-data-item"):
            dt = item.find("dt")
            dd = item.find("dd")
            if dt and dd:
                key = re.sub(
                    r"[^a-zA-Z0-9]+",
                    "_",
                    dt.get_text(strip=True).lower(),
                ).strip("_")
                data["metadata"][f"audience_{key}"] = dd.get_text(strip=True)

    # ── walk article for section-aware extraction ──
    current_section = "Unknown"
    article = soup.find("article", class_="report") or soup

    for el in article.descendants:
        if not isinstance(el, Tag):
            continue

        # Section heading
        if el.name == "div" and "SectionHeading" in el.get("class", []):
            h3 = el.find("h3")
            if h3:
                current_section = h3.get_text(strip=True)

        # SpreadsheetBlockRow — numeric ratings
        if el.name == "div" and "SpreadsheetBlockRow" in el.get("class", []):
            table = el.find("table", class_="block-table")
            if not table:
                continue
            headers = []
            thead = table.find("thead")
            if thead:
                for th in thead.find_all("th"):
                    if "empty-cell" not in th.get("class", []):
                        headers.append(th.get_text(strip=True))
            tbody = table.find("tbody")
            if tbody:
                for tr in tbody.find_all("tr"):
                    cells = tr.find_all(["th", "td"])
                    if len(cells) < 2:
                        continue
                    row = {
                        "section": current_section,
                        "question": cells[0].get_text(strip=True),
                    }
                    for i, c in enumerate(cells[1:]):
                        h = headers[i] if i < len(headers) else f"col_{i}"
                        key = re.sub(r"[^a-zA-Z0-9]+", "_", h.lower()).strip("_")
                        row[key] = c.get_text(strip=True)
                    data["ratings"].append(row)

        # CommentBlockRow
        if el.name == "div" and "CommentBlockRow" in el.get("class", []):
            parent = el.find_parent("div", class_="report-block")
            qtitle = ""
            if parent:
                h4 = parent.find("h4", class_="ReportBlockTitle")
                if h4:
                    for hid in h4.find_all("span", class_="hidden"):
                        hid.decompose()
                    qtitle = h4.get_text(strip=True)
            table = el.find("table", class_="block-table")
            if table:
                tbody = table.find("tbody")
                if tbody:
                    for tr in tbody.find_all("tr"):
                        td = tr.find("td")
                        if td:
                            txt = td.get_text(strip=True)
                            if txt and txt != "[No Response]":
                                data["comments"].append(
                                    {
                                        "section": current_section,
                                        "question": qtitle,
                                        "comment": txt,
                                    }
                                )

        # FrequencyBlockRow — demographics
        if el.name == "div" and "FrequencyBlockRow" in el.get("class", []):
            qt = el.find("h4", class_="FrequencyQuestionTitle")
            if not qt:
                qt = el.find("span", id=re.compile(r"_qItemTitle"))
            qtext = qt.get_text(strip=True) if qt else ""
            fd = el.find("div", class_="frequency-data")
            if fd:
                for li in fd.find_all("li"):
                    ct = li.find("div", class_="frequency-data-item-choice-text")
                    cn = li.find("div", class_="frequency-data-item-choice-nb")
                    cp = li.find("div", class_="frequency-data-item-choice-per")
                    if ct:
                        data["demographics"].append(
                            {
                                "section": "Demographics",
                                "question": qtext,
                                "choice": ct.get_text(strip=True),
                                "count": cn.get_text(strip=True) if cn else "0",
                                "percentage": cp.get_text(strip=True) if cp else "0%",
                            }
                        )

    # ── individual responses (RespS_Sheet) ──
    for idx, sheet in enumerate(soup.find_all("div", class_="RespS_Sheet")):
        course = instr = ""
        for ts in sheet.find_all("span", class_="RespS_Title"):
            t = ts.get_text(strip=True)
            if "Courses Name:" in t:
                course = t.replace("Courses Name:", "").strip()
            elif "Instructors Name:" in t:
                instr = t.replace("Instructors Name:", "").strip()

        for q_item in sheet.find_all("li", class_="RespS_QuestionTitle_ListItem"):
            qi_span = q_item.find("span", class_="RespS_QuestionTitle_index")
            qi = qi_span.get_text(strip=True).rstrip(".") if qi_span else ""

            qdiv = q_item.find("div", class_="RespS_QuestionTitle_font")
            qt = ""
            if qdiv:
                for hid in qdiv.find_all("span", class_="hidden"):
                    hid.decompose()
                qt = qdiv.get_text(strip=True)
                qt = re.sub(r"^\d+\.\s*", "", qt)

            subs = q_item.find_all("span", class_="RespS_QuestionRow_font")
            if subs:
                for sq in subs:
                    resp_ul = sq.find_next_sibling("ul")
                    rf = (
                        resp_ul.find("span", class_="RespS_Resp_font")
                        if resp_ul
                        else None
                    )
                    data["individual_responses"].append(
                        {
                            "respondent_id": idx + 1,
                            "course": course,
                            "instructor": instr,
                            "question_index": qi,
                            "question_group": qt,
                            "question": sq.get_text(strip=True),
                            "response": rf.get_text(strip=True) if rf else "",
                        }
                    )
            else:
                for rf in q_item.find_all("span", class_="RespS_Resp_font"):
                    data["individual_responses"].append(
                        {
                            "respondent_id": idx + 1,
                            "course": course,
                            "instructor": instr,
                            "question_index": qi,
                            "question_group": "",
                            "question": qt,
                            "response": rf.get_text(strip=True),
                        }
                    )
    return data


# ── SAVE (JSON only) ────────────────────────────────
def save_checkpoint(all_data, output_dir):
    with open(output_dir / "checkpoint_data.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)


def save_final(all_data, output_dir):
    # Master file
    with open(output_dir / "all_data.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    # Per-report files
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(exist_ok=True)
    for rpt in all_data["reports"]:
        rid = rpt["metadata"].get("report_id", "unknown")
        safe = re.sub(r"[^\w\-.]", "_", rid)
        with open(reports_dir / f"{safe}.json", "w", encoding="utf-8") as f:
            json.dump(rpt, f, indent=2, ensure_ascii=False)

    # Flat views
    flat = {
        "all_metadata": [],
        "all_ratings": [],
        "all_comments": [],
        "all_demographics": [],
        "all_individual_responses": [],
    }
    for rpt in all_data["reports"]:
        flat["all_metadata"].append(rpt["metadata"])
        flat["all_ratings"].extend(rpt["ratings"])
        flat["all_comments"].extend(rpt["comments"])
        flat["all_demographics"].extend(rpt["demographics"])
        flat["all_individual_responses"].extend(rpt["individual_responses"])

    with open(output_dir / "flat_extracted.json", "w", encoding="utf-8") as f:
        json.dump(flat, f, indent=2, ensure_ascii=False)


# ── RESUME HELPERS ──────────────────────────────────
def find_latest_output_dir():
    candidates = sorted(Path(".").glob("trace_scrape_*"), reverse=True)
    for d in candidates:
        if (d / "checkpoint_data.json").exists():
            return d
    return None


def load_checkpoint(output_dir):
    with open(output_dir / "checkpoint_data.json", encoding="utf-8") as f:
        return json.load(f)


# ── MAIN ────────────────────────────────────────────
def crawl_all_reports(resume_dir=None):
    if resume_dir:
        output_dir = Path(resume_dir)
        all_data = load_checkpoint(output_dir)
        processed_urls = {r["url"] for r in all_data["reports"]}
        print(f"📂 Resuming: {len(processed_urls)} reports already scraped")
    else:
        output_dir = OUTPUT_DIR
        output_dir.mkdir(exist_ok=True)
        all_data = {
            "reports": [],
            "summary": {
                "total_reports": 0,
                "total_ratings": 0,
                "total_comments": 0,
                "total_demographic_entries": 0,
                "total_individual_responses": 0,
            },
        }
        processed_urls = set()

    driver = create_driver()

    try:
        authenticate(driver)
        session = build_requests_session(driver)

        # ── Collect URLs ────────────────────
        print("\n🔍 Collecting report URLs from list pages...")
        all_urls = collect_report_links(driver)
        print(f"\n📊 Found {len(all_urls)} report URLs")

        if not all_urls:
            print("❌ No report URLs found — check that the list page loads correctly.")
            print("   Debug: saving page source for inspection...")
            with open(output_dir / "debug_list_page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            driver.quit()
            return

        # Save URL list
        with open(output_dir / "report_urls.json", "w", encoding="utf-8") as f:
            json.dump(
                {"total_urls": len(all_urls), "urls": all_urls},
                f,
                indent=2,
                ensure_ascii=False,
            )

        # Filter already-processed
        remaining = [u for u in all_urls if u not in processed_urls]
        print(f"⏭  {len(processed_urls)} already done — {len(remaining)} remaining")

        # ── Fetch & parse each report ────────
        total = len(remaining)
        for idx, url in enumerate(remaining, 1):
            short = (
                url.split("SelectedIDforPrint=")[-1][:12]
                if "SelectedIDforPrint" in url
                else str(idx)
            )
            print(f"📄 Report {idx}/{total} (id:{short}...) — ", end="", flush=True)

            html = None
            for attempt in range(MAX_RETRIES):
                try:
                    resp = session.get(url, timeout=30)
                    # Detect session expiry
                    if "login.microsoftonline.com" in str(resp.url):
                        print("session expired — re-auth... ", end="", flush=True)
                        authenticate(driver)
                        session = build_requests_session(driver)
                        continue
                    if resp.status_code == 200 and len(resp.text) > 500:
                        html = resp.text
                        break
                    print(f"status {resp.status_code} ", end="", flush=True)
                except requests.RequestException:
                    print("network err ", end="", flush=True)
                time.sleep(2)

            # Selenium fallback
            if not html:
                print("Selenium fallback... ", end="", flush=True)
                try:
                    driver.get(url)
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "article.report, div.report")
                        )
                    )
                    html = driver.page_source
                    session = build_requests_session(driver)
                except TimeoutException:
                    print("FAILED (timeout)")
                    continue

            if not html:
                print("FAILED (no HTML)")
                continue

            try:
                rpt = parse_report(html, url)

                cc = rpt["metadata"].get("course_code", f"unknown_{idx}")
                sem = rpt["metadata"].get("semester", "unknown")
                inst = rpt["metadata"].get("instructor", "unknown")
                rpt["metadata"]["report_id"] = f"{sem}_{cc}_{inst}"
                rpt["metadata"]["url"] = url

                for coll in [
                    rpt["ratings"],
                    rpt["comments"],
                    rpt["demographics"],
                    rpt["individual_responses"],
                ]:
                    for item in coll:
                        item["report_id"] = rpt["metadata"]["report_id"]
                        item["course_code"] = cc
                        item["semester"] = sem
                        item["instructor"] = inst
                        item["course_name"] = rpt["metadata"].get("course_name", "")

                all_data["reports"].append(rpt)
                all_data["summary"]["total_reports"] += 1
                all_data["summary"]["total_ratings"] += len(rpt["ratings"])
                all_data["summary"]["total_comments"] += len(rpt["comments"])
                all_data["summary"]["total_demographic_entries"] += len(
                    rpt["demographics"]
                )
                all_data["summary"]["total_individual_responses"] += len(
                    rpt["individual_responses"]
                )

                print(
                    f"✅ {len(rpt['ratings'])} ratings, "
                    f"{len(rpt['comments'])} comments, "
                    f"{len(rpt['individual_responses'])} responses"
                )

            except Exception as exc:
                print(f"PARSE ERROR: {exc}")

            # Checkpoint
            if idx % CHECKPOINT_EVERY == 0:
                save_checkpoint(all_data, output_dir)
                print(f"  💾 Checkpoint ({idx}/{total})")

            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    except KeyboardInterrupt:
        print(f"\n⛔ Interrupted — saving progress...")

    finally:
        save_final(all_data, output_dir)
        s = all_data["summary"]
        print(f"\n✅ Done! Data saved to {output_dir}/")
        print(f"   Reports:          {s['total_reports']}")
        print(f"   Ratings:          {s['total_ratings']}")
        print(f"   Comments:         {s['total_comments']}")
        print(f"   Demographics:     {s['total_demographic_entries']}")
        print(f"   Indiv. responses: {s['total_individual_responses']}")
        driver.quit()


# ── ENTRY ───────────────────────────────────────────
if __name__ == "__main__":
    import sys

    resume = None
    if len(sys.argv) > 1 and sys.argv[1] == "--resume":
        resume = find_latest_output_dir()
        if resume:
            print(f"📂 Found checkpoint in {resume}")
        else:
            print("No checkpoint found — starting fresh.")

    crawl_all_reports(resume_dir=resume)