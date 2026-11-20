import streamlit as st
import pandas as pd
import requests
import base64
import statistics
import re
from datetime import datetime, timezone, timedelta

# --- Page Config ---
st.set_page_config(page_title="Grassi Cards: Pro Arbitrage Scanner", layout="wide")
st.title("📈 Grassi Cards: Pro Arbitrage Scanner")

DEFAULT_BASE_EXCLUDES = (
    "refractor, auto, autograph, mojo, shimmer, speckle, raywave, sapphire, mega, "
    "silver, holo, x-fractor, xfractor, atomic, lava, wave, ice, hyper, "
    "scope, disco, mosaic, checkerboard, tiger, zebra, lazer, pulsar, snakeskin, "
    "negative, variation, genesis, pandora, gold, blue, green, purple, "
    "orange, red, pink, black, aqua, yellow, fuchsia, platinum, superfractor, "
    "printing plate, numbered"
)

RAW_GRADING_EXCLUDES = ["psa", "bgs", "sgc", "cgc", "csg", "hga", "beckett", "graded", "slab", "slabbed"]

def load_secret(key_name):
    """Safely reads from st.secrets without crashing if no secrets file exists."""
    try:
        return st.secrets.get(key_name, "")
    except Exception:
        return ""

# --- Sidebar: API & Strategy Settings ---
with st.sidebar:
    st.header("🔑 eBay Browse API Credentials")
    EBAY_APP_ID = st.text_input(
        "App ID (Client ID)",
        value=load_secret("EBAY_APP_ID"),
        type="password"
    ).strip()
    EBAY_CERT_ID = st.text_input(
        "Cert ID (Client Secret)",
        value=load_secret("EBAY_CERT_ID"),
        type="password"
    ).strip()

    st.divider()
    st.header("📊 SoldComps API (Auto-Comps)")
    SOLDCOMPS_API_KEY = st.text_input(
        "SoldComps API Key (Optional)",
        value=load_secret("SOLDCOMPS_API_KEY"),
        type="password",
        help="Leave blank to enter sold comps manually."
    ).strip()

    st.divider()
    st.header("⚙️ Deal & Filter Settings")
    discount_pct = st.slider("Target Discount Below Comp (%)", 5, 35, 10)
    default_slab_shipping = st.number_input("Fallback Graded Slab Shipping ($)", value=6.00, step=0.50)
    default_raw_shipping = st.number_input("Fallback Raw Card Shipping ($)", value=1.50, step=0.50)
    stale_days_threshold = st.number_input("Stale Inventory Threshold (Days)", value=30, step=5)
    casual_seller_max_fb = st.number_input("Casual Seller Max Feedback", value=100, step=25)
    max_auction_bids = st.number_input("Low-Bid Auction Max Bids", value=5, step=1)
    auction_hours_left = st.number_input("Auctions Ending Within (Hours)", value=3, step=1)

# --- Parallel, Card Number, Grade & Query Filtering Helpers ---
def get_effective_excludes(exclude_csv, player="", card_set="", specific_parallel=""):
    """Prevents an exclusion word from blocking terms that belong to the Set Name or Player Name."""
    if not exclude_csv.strip():
        return []
    protected_text = f"{player} {card_set} {specific_parallel}".lower()
    protected_words = set(re.findall(r"\b\w+\b", protected_text))

    effective = []
    for word in exclude_csv.split(","):
        w = word.strip()
        if not w:
            continue
        if w.lower() in protected_words or re.search(rf"\b{re.escape(w)}\b", protected_text, re.IGNORECASE):
            continue
        effective.append(w)
    return effective

def build_search_query(player, card_set, condition_type, grade, mode, specific_parallel, exclude_csv, card_num=""):
    """Builds the search query for Graded or Raw cards with card number and negative exclusions."""
    parts = [player.strip()]
    if card_set and card_set.strip():
        parts.append(card_set.strip())
    if card_num and card_num.strip():
        parts.append(card_num.strip().lstrip("#").strip())
    if mode == "Specific Parallel" and specific_parallel.strip():
        parts.append(specific_parallel.strip())
    if condition_type == "Graded" and grade and grade.strip():
        parts.append(grade.strip())

    query = " ".join(parts)

    neg_tokens = []
    if condition_type == "Raw (Ungraded)":
        neg_tokens.extend([f"-{g}" for g in ["PSA", "BGS", "SGC", "CGC", "CSG", "HGA", "graded", "slab"]])

    if mode in ("Base Only", "Specific Parallel"):
        excludes = get_effective_excludes(exclude_csv, player, card_set, specific_parallel)
        single_word_excludes = [w for w in excludes if " " not in w]
        neg_tokens.extend([f"-{w}" for w in single_word_excludes])

    # Keep total query safely within eBay's 350-character limit
    for tok in neg_tokens[:22]:
        candidate = f"{query} {tok}".strip()
        if len(candidate) <= 320:
            query = candidate
        else:
            break

    return query

def matches_card_number(title, card_num):
    """Matches #307, # 307, 307, or BCP-50 while ignoring serial numbers like /307."""
    if not card_num or not card_num.strip():
        return True
    clean_num = card_num.strip().lstrip("#").strip()
    pattern = rf"(?<!/)(?:^|[\s#(-])#?\s*{re.escape(clean_num)}\b"
    return bool(re.search(pattern, title, re.IGNORECASE))

def matches_grade_condition(title, condition_type, grade):
    """Enforces target grade for Graded searches, or rejects slabs for Raw searches."""
    if condition_type == "Raw (Ungraded)":
        for g_word in RAW_GRADING_EXCLUDES:
            if re.search(rf"\b{re.escape(g_word)}\b", title, re.IGNORECASE):
                return False
        return True

    if condition_type == "Graded" and grade and grade.strip():
        grade_parts = [re.escape(p) for p in grade.strip().split()]
        grade_pattern = r"\b" + r"\s*".join(grade_parts) + r"\b"
        return bool(re.search(grade_pattern, title, re.IGNORECASE))

    return True

def matches_card_filter(
    title, condition_type, grade, mode, specific_parallel,
    exclude_csv, exclude_numbered, player="", card_set="", card_num=""
):
    """Whole-word title filter enforcing condition/grade, card number, and parallel rules."""
    if not title:
        return False
    if not matches_grade_condition(title, condition_type, grade):
        return False
    if not matches_card_number(title, card_num):
        return False

    if mode == "Specific Parallel" and specific_parallel.strip():
        if not re.search(rf"\b{re.escape(specific_parallel.strip())}\b", title, re.IGNORECASE):
            return False

    if mode in ("Base Only", "Specific Parallel"):
        if exclude_numbered and re.search(r"(?:/|\bout of\s*)\d+", title, re.IGNORECASE):
            return False

        for w in get_effective_excludes(exclude_csv, player, card_set, specific_parallel):
            if re.search(rf"\b{re.escape(w)}\b", title, re.IGNORECASE):
                return False

    return True

# --- eBay Browse API Helper Functions ---
@st.cache_data(ttl=6000)
def get_ebay_oauth_token(app_id, cert_id):
    """Generates an OAuth Application Access Token for the eBay Browse API."""
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    credentials = f"{app_id}:{cert_id}"
    encoded_creds = base64.b64encode(credentials.encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded_creds}",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    resp = requests.post(url, headers=headers, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]

def search_browse_api(token, query, filter_str="", sort="newlyListed", limit=50):
    """Queries eBay Browse API item_summary/search endpoint."""
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": query,
        "limit": limit,
        "sort": sort,
    }
    if filter_str:
        params["filter"] = filter_str
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def get_item_watch_count(token, item_id):
    """Fetches individual item details to retrieve watchCount if available."""
    url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("watchCount", "N/A")
    except Exception:
        pass
    return "N/A"

def fetch_sold_comps_auto(
    api_key, search_query, fallback_ship, condition_type, grade, mode, specific_parallel,
    exclude_csv, exclude_numbered, player="", card_set="", card_num=""
):
    """Fetches recent sold listings from SoldComps, filters to exact card/grade/parallel, and returns last 10 comps + 30d volume."""
    url = "https://api.sold-comps.com/v1/scrape"
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    params = {"keyword": search_query, "ebaySite": "ebay.com"}

    resp = requests.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or data.get("results") or []

    totals = []
    sold_30d = 0
    cutoff_30d = datetime.now(timezone.utc).date() - timedelta(days=30)

    for item in items:
        title = item.get("title", "")
        if not matches_card_filter(
            title, condition_type, grade, mode, specific_parallel, exclude_csv, exclude_numbered,
            player=player, card_set=card_set, card_num=card_num
        ):
            continue

        raw_price = item.get("soldPrice") or item.get("price") or 0.0
        raw_ship = item.get("shippingPrice") if item.get("shippingPrice") is not None else item.get("shipping")
        price = float(raw_price) if raw_price else 0.0
        ship = float(raw_ship) if raw_ship is not None else fallback_ship

        if price > 0:
            totals.append(price + ship)

        sold_date_str = item.get("endedAt") or item.get("date") or item.get("soldDate")
        if sold_date_str:
            try:
                sold_dt = datetime.fromisoformat(sold_date_str[:10]).date()
                if sold_dt >= cutoff_30d:
                    sold_30d += 1
            except ValueError:
                pass

    return totals[:10], sold_30d

def parse_total_cost(item, fallback_ship):
    price = float(item.get("price", {}).get("value", 0.0))
    ship_opts = item.get("shippingOptions", [])
    if ship_opts and "shippingCost" in ship_opts[0]:
        shipping = float(ship_opts[0]["shippingCost"].get("value", fallback_ship))
    else:
        shipping = fallback_ship
    return price, shipping, price + shipping

def days_since_listed(creation_date_str):
    if not creation_date_str:
        return None
    dt = datetime.fromisoformat(creation_date_str.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days

def is_off_peak_est(end_date_str):
    """Checks if an auction ends between 2:00 AM and 7:00 AM EST."""
    if not end_date_str:
        return False
    dt_utc = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    dt_est = dt_utc - timedelta(hours=5)
    return 2 <= dt_est.hour < 7

# --- Main Input Section ---
cond_col1, cond_col2 = st.columns([1.5, 2.5])
with cond_col1:
    card_condition = st.radio(
        "Card Condition",
        ["Graded", "Raw (Ungraded)"],
        horizontal=True
    )

target_grade = ""
if card_condition == "Graded":
    with cond_col2:
        g_col1, g_col2 = st.columns([1.5, 1.5])
        with g_col1:
            grade_choice = st.selectbox(
                "Target Grade",
                ["PSA 10", "PSA 9", "PSA 8", "BGS 9.5", "BGS 9", "SGC 10", "SGC 9.5", "CGC 10", "Custom..."],
                index=0
            )
        if grade_choice == "Custom...":
            with g_col2:
                target_grade = st.text_input("Custom Grade", placeholder="e.g., PSA 7, BGS 10, TAG 10").strip()
        else:
            target_grade = grade_choice

active_fallback_ship = default_slab_shipping if card_condition == "Graded" else default_raw_shipping

col1, col2, col3, col4 = st.columns([2, 2, 1.3, 1.7])
with col1:
    player_name = st.text_input("Player Name", placeholder="e.g., Joe Burrow")
with col2:
    set_name = st.text_input("Set Name", placeholder="e.g., 2020 Prizm")
with col3:
    card_number = st.text_input("Card # (Optional)", placeholder="e.g., 307 or BCP-50")
with col4:
    parallel_mode = st.selectbox(
        "Parallel / Variation Filter",
        ["Base Only", "Specific Parallel", "All Parallels"],
        index=0
    )

specific_parallel = ""
if parallel_mode == "Specific Parallel":
    specific_parallel = st.text_input(
        "Target Parallel Keyword",
        placeholder="e.g., Silver, Mojo, Blue Refractor, Lazer"
    )

if parallel_mode in ("Base Only", "Specific Parallel"):
    with st.expander("🛠️ Parallel Exclusion Keywords (Customize)", expanded=(parallel_mode == "Specific Parallel")):
        default_excl = DEFAULT_BASE_EXCLUDES if parallel_mode == "Base Only" else "auto, autograph, printing plate"
        exclude_keywords = st.text_area(
            "Exclude listings containing any of these words (comma-separated):",
            value=default_excl,
            height=70
        )
        exclude_numbered = st.checkbox("Exclude serial-numbered cards (e.g., /499, /99, /25)", value=True)
else:
    exclude_keywords = ""
    exclude_numbered = False

c_comp1, c_comp2 = st.columns([3, 2])
with c_comp1:
    comps_input = st.text_input(
        "Manual Sold Comps Override (Optional, up to last 10 sales, comma-separated $)",
        placeholder="Auto-filled if SoldComps key is set",
        help="Leave blank to use SoldComps automatically, or paste up to 10 recent sold totals manually."
    )
with c_comp2:
    sold_30d_count = st.number_input(
        "Manual 30-Day Sold Volume (Auto-updated w/ SoldComps)",
        min_value=0, value=10, step=1
    )

fetch_watchers = st.checkbox(
    "Fetch Watcher Counts for Flagged Deals (uses 1 extra eBay API call per deal)",
    value=True
)

# --- Run Scanner ---
if st.button("Scan eBay Market", type="primary"):
    if not EBAY_APP_ID or not EBAY_CERT_ID:
        st.error("Please enter your eBay App ID and Cert ID in the sidebar.")
    elif not player_name or not set_name:
        st.warning("Please enter both a Player Name and Set Name.")
    elif card_condition == "Graded" and not target_grade:
        st.warning("Please enter a Custom Grade (or pick one from the Target Grade dropdown).")
    elif parallel_mode == "Specific Parallel" and not specific_parallel.strip():
        st.warning("Please enter a Target Parallel Keyword (or switch to Base Only).")
    else:
        base_query = build_search_query(
            player_name, set_name, card_condition, target_grade,
            parallel_mode, specific_parallel, exclude_keywords, card_num=card_number
        )
        condition_label = target_grade if card_condition == "Graded" else "Raw (Ungraded)"
        st.caption(f"Scanning **{condition_label}** | Query sent to APIs: `{base_query}`")

        # 1. Get Last 10 Sold Comps (Auto via SoldComps or Manual Override)
        raw_comps = []
        if SOLDCOMPS_API_KEY and not comps_input.strip():
            try:
                with st.spinner("Pulling & filtering last 10 sold comps from SoldComps..."):
                    raw_comps, auto_sold_30d = fetch_sold_comps_auto(
                        SOLDCOMPS_API_KEY, base_query, active_fallback_ship,
                        card_condition, target_grade, parallel_mode, specific_parallel,
                        exclude_keywords, exclude_numbered,
                        player=player_name, card_set=set_name, card_num=card_number
                    )
                    if auto_sold_30d > 0:
                        sold_30d_count = auto_sold_30d
            except Exception as e:
                st.warning(f"SoldComps lookup failed ({e}). Falling back to manual comps.")
        elif comps_input.strip():
            try:
                raw_comps = [float(x.strip().replace("$", "")) for x in comps_input.split(",") if x.strip()][:10]
            except ValueError:
                st.error("Invalid format in Sold Comps. Use numbers separated by commas.")
                st.stop()

        # Drop Highest & Lowest of Last 10 Sales to Determine Average Comp
        recent_sales = raw_comps[:10]
        trimmed_comps = []
        if len(recent_sales) >= 3:
            sorted_comps = sorted(recent_sales)
            dropped_low = sorted_comps[0]
            dropped_high = sorted_comps[-1]
            trimmed_comps = sorted_comps[1:-1]
            avg_comp = sum(trimmed_comps) / len(trimmed_comps)
            median_comp = statistics.median(recent_sales)
            st.caption(
                f"Last {len(recent_sales)} comps: {', '.join(f'${c:.2f}' for c in recent_sales)} "
                f"| Dropped Low: **${dropped_low:.2f}** & High: **${dropped_high:.2f}** "
                f"(Averaging middle {len(trimmed_comps)} sales)"
            )
        elif recent_sales:
            trimmed_comps = recent_sales
            avg_comp = sum(recent_sales) / len(recent_sales)
            median_comp = statistics.median(recent_sales)
            st.caption(
                f"Found {len(recent_sales)} comp(s): {', '.join(f'${c:.2f}' for c in recent_sales)} "
                f"(Need at least 3 sales to drop high & low)"
            )
        else:
            avg_comp = 0.0
            median_comp = 0.0

        target_buy_price = avg_comp * (1 - discount_pct / 100.0) if avg_comp > 0 else 0.0

        try:
            with st.spinner("Connecting to eBay Browse API & scanning all 8 signals..."):
                token = get_ebay_oauth_token(EBAY_APP_ID, EBAY_CERT_ID)

                # Active Buy It Now Listings
                bin_data = search_browse_api(
                    token, base_query, filter_str="buyingOptions:{FIXED_PRICE}", sort="newlyListed", limit=100
                )
                raw_bin_items = bin_data.get("itemSummaries", [])
                bin_items = [
                    it for it in raw_bin_items
                    if matches_card_filter(
                        it.get("title", ""), card_condition, target_grade, parallel_mode, specific_parallel,
                        exclude_keywords, exclude_numbered,
                        player=player_name, card_set=set_name, card_num=card_number
                    )
                ]
                total_active = len(bin_items) if len(raw_bin_items) < 100 else bin_data.get("total", len(bin_items))

                # Sell-Through Rate
                str_pct = (sold_30d_count / total_active * 100) if total_active > 0 else 0.0

                # Top-Level KPI Row
                m1, m2, m3, m4 = st.columns(4)
                m1.metric(
                    "Avg Comp (Excl. High/Low)",
                    f"${avg_comp:.2f}" if avg_comp else "Not Set",
                    f"Middle {len(trimmed_comps)} of {len(recent_sales)} sales" if len(recent_sales) >= 3 else None
                )
                m2.metric("Median Comp", f"${median_comp:.2f}" if median_comp else "Not Set")
                m3.metric(f"Target Buy ({discount_pct}% Off)", f"${target_buy_price:.2f}" if target_buy_price else "Not Set")
                m4.metric("Sell-Through Rate", f"{str_pct:.1f}%", f"{sold_30d_count} sold / {total_active} active")

                # --- Tabs for All 8 Metrics ---
                tab1, tab2, tab3, tab4 = st.tabs([
                    "🎯 Buy It Now & Stale Best Offers",
                    "⏰ Off-Peak / Low-Bid Auctions",
                    "⚖️ Cross-Grade & Raw Spreads",
                    "🔍 Broad / Missing Specifics"
                ])

                # TAB 1: BIN Deals, Best Offer + Stale Inventory, Watchers, Casual Sellers
                with tab1:
                    rows = []
                    for item in bin_items:
                        price, ship, total = parse_total_cost(item, active_fallback_ship)
                        buying_opts = item.get("buyingOptions", [])
                        has_bo = "BEST_OFFER" in buying_opts
                        age_days = days_since_listed(item.get("itemCreationDate"))
                        is_stale_bo = has_bo and (age_days is not None and age_days >= stale_days_threshold)

                        fb_score = item.get("seller", {}).get("feedbackScore", 9999)
                        is_casual = fb_score <= casual_seller_max_fb
                        is_price_deal = (target_buy_price > 0 and total <= target_buy_price)

                        flags = []
                        if is_price_deal:
                            flags.append("🔥 Under Target")
                        if is_stale_bo:
                            flags.append("📉 Stale Best Offer")
                        if is_casual:
                            flags.append("👤 Casual Seller")

                        watchers = "—"
                        if fetch_watchers and flags:
                            watchers = get_item_watch_count(token, item["itemId"])

                        rows.append({
                            "Signals": " | ".join(flags) if flags else "Standard",
                            "Title": item.get("title", ""),
                            "Total ($)": round(total, 2),
                            "Best Offer": "✅" if has_bo else "❌",
                            "Days Listed": age_days if age_days is not None else "N/A",
                            "Watchers": watchers,
                            "Seller FB": fb_score,
                            "URL": item.get("itemWebUrl", "")
                        })

                    if rows:
                        df_bin = pd.DataFrame(rows).sort_values(by="Total ($)")
                        st.dataframe(
                            df_bin,
                            column_config={"URL": st.column_config.LinkColumn("eBay Link")},
                            hide_index=True,
                            use_container_width=True
                        )
                    else:
                        st.info(f"No active {condition_label} Buy It Now listings found matching this card and parallel.")

                # TAB 2: Off-Peak Auctions with Low Bids
                with tab2:
                    now_utc = datetime.now(timezone.utc)
                    end_window = (now_utc + timedelta(hours=auction_hours_left)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    auc_filter = f"buyingOptions:{{AUCTION}},itemEndDate:[..{end_window}]"
                    auc_data = search_browse_api(token, base_query, filter_str=auc_filter, sort="endingSoonest", limit=50)
                    auc_items = [
                        it for it in auc_data.get("itemSummaries", [])
                        if matches_card_filter(
                            it.get("title", ""), card_condition, target_grade, parallel_mode, specific_parallel,
                            exclude_keywords, exclude_numbered,
                            player=player_name, card_set=set_name, card_num=card_number
                        )
                    ]

                    auc_rows = []
                    for item in auc_items:
                        bids = item.get("bidCount", 0)
                        if bids <= max_auction_bids:
                            _, _, total = parse_total_cost(item, active_fallback_ship)
                            end_str = item.get("itemEndDate", "")
                            off_peak = is_off_peak_est(end_str)
                            auc_rows.append({
                                "Off-Peak (2–7 AM EST)": "🌙 Yes" if off_peak else "No",
                                "Title": item.get("title", ""),
                                "Current Bid + Ship ($)": round(total, 2),
                                "Bids": bids,
                                "Ends (UTC)": end_str,
                                "URL": item.get("itemWebUrl", "")
                            })

                    if auc_rows:
                        st.dataframe(
                            pd.DataFrame(auc_rows),
                            column_config={"URL": st.column_config.LinkColumn("eBay Link")},
                            hide_index=True,
                            use_container_width=True
                        )
                    else:
                        st.info(f"No low-bid {condition_label} auctions ending within {auction_hours_left} hours.")

                # TAB 3: Cross-Grade & Raw Spreads
                with tab3:
                    benchmark_targets = [
                        ("Graded", "PSA 10"),
                        ("Graded", "PSA 9"),
                        ("Graded", "BGS 9.5"),
                        ("Graded", "SGC 10"),
                        ("Raw (Ungraded)", "")
                    ]
                    spread_rows = []
                    for b_cond, b_grade in benchmark_targets:
                        if b_cond == card_condition and b_grade.lower() == target_grade.lower():
                            continue

                        b_label = b_grade if b_cond == "Graded" else "Raw (Ungraded)"
                        b_ship = default_slab_shipping if b_cond == "Graded" else default_raw_shipping
                        b_query = build_search_query(
                            player_name, set_name, b_cond, b_grade, parallel_mode, specific_parallel,
                            exclude_keywords, card_num=card_number
                        )
                        b_data = search_browse_api(
                            token, b_query, filter_str="buyingOptions:{FIXED_PRICE}", sort="price", limit=15
                        )
                        b_items = [
                            it for it in b_data.get("itemSummaries", [])
                            if matches_card_filter(
                                it.get("title", ""), b_cond, b_grade, parallel_mode, specific_parallel,
                                exclude_keywords, exclude_numbered,
                                player=player_name, card_set=set_name, card_num=card_number
                            )
                        ]
                        if b_items:
                            _, _, lowest_total = parse_total_cost(b_items[0], b_ship)
                            if avg_comp > 0:
                                diff = (lowest_total - avg_comp) if card_condition == "Raw (Ungraded)" else (avg_comp - lowest_total)
                                diff_display = round(diff, 2)
                            else:
                                diff_display = "Set Comps"

                            spread_col_name = (
                                "Upside vs Raw Avg Comp ($)"
                                if card_condition == "Raw (Ungraded)"
                                else f"Discount vs {condition_label} Avg Comp ($)"
                            )
                            spread_rows.append({
                                "Condition / Grade": b_label,
                                "Lowest BIN + Ship ($)": round(lowest_total, 2),
                                spread_col_name: diff_display,
                                "Top Listing": b_items[0].get("title", ""),
                                "URL": b_items[0].get("itemWebUrl", "")
                            })
                    if spread_rows:
                        st.dataframe(
                            pd.DataFrame(spread_rows),
                            column_config={"URL": st.column_config.LinkColumn("eBay Link")},
                            hide_index=True,
                            use_container_width=True
                        )
                    else:
                        st.info("No comparable cross-grade or raw listings found for this card and parallel.")

                # TAB 4: Broad Search / Missing Item Specifics
                with tab4:
                    last_name = player_name.strip().split()[-1]
                    broad_query = build_search_query(
                        last_name, "", card_condition, target_grade, parallel_mode, specific_parallel,
                        exclude_keywords, card_num=card_number
                    )
                    broad_data = search_browse_api(
                        token, broad_query, filter_str="buyingOptions:{FIXED_PRICE}", sort="newlyListed", limit=50
                    )
                    broad_rows = []
                    set_tokens = [t for t in set_name.lower().split() if not t.isdigit()]
                    set_keyword = set_tokens[0] if set_tokens else set_name.lower().split()[0]

                    for item in broad_data.get("itemSummaries", []):
                        title = item.get("title", "")
                        if not matches_card_filter(
                            title, card_condition, target_grade, parallel_mode, specific_parallel,
                            exclude_keywords, exclude_numbered,
                            player=player_name, card_set=set_name, card_num=card_number
                        ):
                            continue
                        if set_keyword not in title.lower() or len(title) < 40:
                            _, _, total = parse_total_cost(item, active_fallback_ship)
                            broad_rows.append({
                                "Title": title,
                                "Total ($)": round(total, 2),
                                "Reason Flagged": "Missing Set Keyword / Short Title",
                                "URL": item.get("itemWebUrl", "")
                            })
                    if broad_rows:
                        st.dataframe(
                            pd.DataFrame(broad_rows),
                            column_config={"URL": st.column_config.LinkColumn("eBay Link")},
                            hide_index=True,
                            use_container_width=True
                        )
                    else:
                        st.info(f"No poorly titled or missing-specifics {condition_label} listings flagged.")

        except Exception as e:
            st.error(f"eBay API Error: {e}")
