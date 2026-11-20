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
    "silver, prizm, holo, x-fractor, xfractor, atomic, lava, wave, ice, hyper, "
    "scope, disco, mosaic, checkerboard, tiger, zebra, gold, blue, green, purple, "
    "orange, red, pink, black, aqua, yellow, fuchsia, platinum, superfractor, "
    "printing plate, numbered"
)

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
    default_shipping = st.number_input("Fallback Slab Shipping ($)", value=6.00, step=0.50)
    stale_days_threshold = st.number_input("Stale Inventory Threshold (Days)", value=30, step=5)
    casual_seller_max_fb = st.number_input("Casual Seller Max Feedback", value=100, step=25)
    max_auction_bids = st.number_input("Low-Bid Auction Max Bids", value=5, step=1)
    auction_hours_left = st.number_input("Auctions Ending Within (Hours)", value=3, step=1)

# --- Parallel & Query Filtering Helpers ---
def build_search_query(player, card_set, grade, mode, specific_parallel, exclude_csv):
    """Builds the search query with positive parallel terms and API negative exclusions."""
    parts = [player.strip(), card_set.strip()]
    if mode == "Specific Parallel" and specific_parallel.strip():
        parts.append(specific_parallel.strip())
    if grade:
        parts.append(grade)

    query = " ".join(parts)

    # Append negative keywords (keep well under eBay's 350-char query limit)
    if mode in ("Base Only", "Specific Parallel") and exclude_csv.strip():
        excludes = [w.strip() for w in exclude_csv.split(",") if w.strip() and " " not in w.strip()]
        neg_tokens = [f"-{w}" for w in excludes[:18]]
        candidate = f"{query} {' '.join(neg_tokens)}".strip()
        if len(candidate) <= 320:
            query = candidate

    return query

def matches_parallel_filter(title, mode, specific_parallel, exclude_csv, exclude_numbered):
    """Whole-word title filter so 'red' excludes 'Red Refractor' without blocking 'Reds'."""
    if not title:
        return False

    if mode == "Specific Parallel" and specific_parallel.strip():
        if not re.search(rf"\b{re.escape(specific_parallel.strip())}\b", title, re.IGNORECASE):
            return False

    if mode in ("Base Only", "Specific Parallel"):
        if exclude_numbered and re.search(r"(?:/|\bout of\s*)\d+", title, re.IGNORECASE):
            return False

        if exclude_csv.strip():
            for word in exclude_csv.split(","):
                w = word.strip()
                if w and re.search(rf"\b{re.escape(w)}\b", title, re.IGNORECASE):
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

def fetch_sold_comps_auto(api_key, search_query, fallback_ship, mode, specific_parallel, exclude_csv, exclude_numbered):
    """Fetches recent sold listings from SoldComps, filters to the exact parallel, and returns comps + 30d volume."""
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
        if not matches_parallel_filter(title, mode, specific_parallel, exclude_csv, exclude_numbered):
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

    return totals[:7], sold_30d

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
col1, col2, col3 = st.columns([2, 2, 2])
with col1:
    player_name = st.text_input("Player Name", placeholder="e.g., Elly De La Cruz")
with col2:
    set_name = st.text_input("Set Name", placeholder="e.g., 2022 Bowman Chrome")
with col3:
    parallel_mode = st.selectbox(
        "Parallel / Variation Filter",
        ["Base Only", "Specific Parallel", "All Parallels"],
        index=0
    )

specific_parallel = ""
if parallel_mode == "Specific Parallel":
    specific_parallel = st.text_input(
        "Target Parallel Keyword",
        placeholder="e.g., Refractor, Mojo, Blue Refractor, Silver Prizm"
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
        "Manual Sold Comps Override (Optional, comma-separated $)",
        placeholder="Auto-filled if SoldComps key is set",
        help="Leave blank to use SoldComps automatically, or paste 5–7 sold totals manually."
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
    elif parallel_mode == "Specific Parallel" and not specific_parallel.strip():
        st.warning("Please enter a Target Parallel Keyword (or switch to Base Only).")
    else:
        base_query = build_search_query(
            player_name, set_name, "PSA 10", parallel_mode, specific_parallel, exclude_keywords
        )
        st.caption(f"Query sent to APIs: `{base_query}`")

        # 1. Get Sold Comps (Auto via SoldComps or Manual Override)
        raw_comps = []
        if SOLDCOMPS_API_KEY and not comps_input.strip():
            try:
                with st.spinner("Pulling & filtering live sold comps from SoldComps..."):
                    raw_comps, auto_sold_30d = fetch_sold_comps_auto(
                        SOLDCOMPS_API_KEY, base_query, default_shipping,
                        parallel_mode, specific_parallel, exclude_keywords, exclude_numbered
                    )
                    if auto_sold_30d > 0:
                        sold_30d_count = auto_sold_30d
                    if raw_comps:
                        st.caption(f"Filtered recent comps ({len(raw_comps)}): {', '.join(f'${c:.2f}' for c in raw_comps)}")
            except Exception as e:
                st.warning(f"SoldComps lookup failed ({e}). Falling back to manual comps.")
        elif comps_input.strip():
            try:
                raw_comps = [float(x.strip().replace("$", "")) for x in comps_input.split(",") if x.strip()]
            except ValueError:
                st.error("Invalid format in Sold Comps. Use numbers separated by commas.")
                st.stop()

        # Median & Outlier-Protected Trimmed Average
        if len(raw_comps) >= 3:
            median_comp = statistics.median(raw_comps)
            sorted_comps = sorted(raw_comps)
            trimmed_avg = sum(sorted_comps[1:-1]) / len(sorted_comps[1:-1])
        elif raw_comps:
            median_comp = statistics.median(raw_comps)
            trimmed_avg = sum(raw_comps) / len(raw_comps)
        else:
            median_comp = 0.0
            trimmed_avg = 0.0

        target_buy_price = median_comp * (1 - discount_pct / 100.0) if median_comp > 0 else 0.0

        try:
            with st.spinner("Connecting to eBay Browse API & scanning all 8 signals..."):
                token = get_ebay_oauth_token(EBAY_APP_ID, EBAY_CERT_ID)

                # Active Buy It Now Listings (fetch up to 100 so post-filtering still leaves plenty)
                bin_data = search_browse_api(
                    token, base_query, filter_str="buyingOptions:{FIXED_PRICE}", sort="newlyListed", limit=100
                )
                raw_bin_items = bin_data.get("itemSummaries", [])
                bin_items = [
                    it for it in raw_bin_items
                    if matches_parallel_filter(
                        it.get("title", ""), parallel_mode, specific_parallel, exclude_keywords, exclude_numbered
                    )
                ]
                total_active = len(bin_items) if len(raw_bin_items) < 100 else bin_data.get("total", len(bin_items))

                # Sell-Through Rate
                str_pct = (sold_30d_count / total_active * 100) if total_active > 0 else 0.0

                # Top-Level KPI Row
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Median Comp", f"${median_comp:.2f}" if median_comp else "Not Set")
                m2.metric("Trimmed Avg (No Outliers)", f"${trimmed_avg:.2f}" if trimmed_avg else "Not Set")
                m3.metric(f"Target Buy ({discount_pct}% Off)", f"${target_buy_price:.2f}" if target_buy_price else "Not Set")
                m4.metric("Sell-Through Rate", f"{str_pct:.1f}%", f"{sold_30d_count} sold / {total_active} active")

                # --- Tabs for All 8 Metrics ---
                tab1, tab2, tab3, tab4 = st.tabs([
                    "🎯 Buy It Now & Stale Best Offers",
                    "⏰ Off-Peak / Low-Bid Auctions",
                    "⚖️ Cross-Grade Spreads",
                    "🔍 Broad / Missing Specifics"
                ])

                # TAB 1: BIN Deals, Best Offer + Stale Inventory, Watchers, Casual Sellers
                with tab1:
                    rows = []
                    for item in bin_items:
                        price, ship, total = parse_total_cost(item, default_shipping)
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
                        st.info("No active Buy It Now listings found matching this parallel.")

                # TAB 2: Off-Peak Auctions with Low Bids
                with tab2:
                    now_utc = datetime.now(timezone.utc)
                    end_window = (now_utc + timedelta(hours=auction_hours_left)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    auc_filter = f"buyingOptions:{{AUCTION}},itemEndDate:[..{end_window}]"
                    auc_data = search_browse_api(token, base_query, filter_str=auc_filter, sort="endingSoonest", limit=50)
                    auc_items = [
                        it for it in auc_data.get("itemSummaries", [])
                        if matches_parallel_filter(
                            it.get("title", ""), parallel_mode, specific_parallel, exclude_keywords, exclude_numbered
                        )
                    ]

                    auc_rows = []
                    for item in auc_items:
                        bids = item.get("bidCount", 0)
                        if bids <= max_auction_bids:
                            _, _, total = parse_total_cost(item, default_shipping)
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
                        st.info(f"No low-bid auctions ending within {auction_hours_left} hours for this parallel.")

                # TAB 3: Cross-Grade & Cross-Company Spreads (Filtered to Same Parallel)
                with tab3:
                    grades = ["PSA 9", "BGS 9.5", "SGC 10"]
                    spread_rows = []
                    for g in grades:
                        g_query = build_search_query(
                            player_name, set_name, g, parallel_mode, specific_parallel, exclude_keywords
                        )
                        g_data = search_browse_api(
                            token, g_query, filter_str="buyingOptions:{FIXED_PRICE}", sort="price", limit=15
                        )
                        g_items = [
                            it for it in g_data.get("itemSummaries", [])
                            if matches_parallel_filter(
                                it.get("title", ""), parallel_mode, specific_parallel, exclude_keywords, exclude_numbered
                            )
                        ]
                        if g_items:
                            _, _, lowest_total = parse_total_cost(g_items[0], default_shipping)
                            spread = (median_comp - lowest_total) if median_comp > 0 else 0.0
                            spread_rows.append({
                                "Grade": g,
                                "Lowest BIN + Ship ($)": round(lowest_total, 2),
                                "Spread vs PSA 10 Median ($)": round(spread, 2) if median_comp else "Set Comps",
                                "Top Listing": g_items[0].get("title", ""),
                                "URL": g_items[0].get("itemWebUrl", "")
                            })
                    if spread_rows:
                        st.dataframe(
                            pd.DataFrame(spread_rows),
                            column_config={"URL": st.column_config.LinkColumn("eBay Link")},
                            hide_index=True,
                            use_container_width=True
                        )
                    else:
                        st.info("No comparable PSA 9, BGS 9.5, or SGC 10 listings found for this parallel.")

                # TAB 4: Broad Search / Missing Item Specifics
                with tab4:
                    last_name = player_name.strip().split()[-1]
                    broad_query = build_search_query(
                        last_name, "", "PSA 10", parallel_mode, specific_parallel, exclude_keywords
                    )
                    broad_data = search_browse_api(
                        token, broad_query, filter_str="buyingOptions:{FIXED_PRICE}", sort="newlyListed", limit=50
                    )
                    broad_rows = []
                    set_tokens = [t for t in set_name.lower().split() if not t.isdigit()]
                    set_keyword = set_tokens[0] if set_tokens else set_name.lower().split()[0]

                    for item in broad_data.get("itemSummaries", []):
                        title = item.get("title", "")
                        if not matches_parallel_filter(title, parallel_mode, specific_parallel, exclude_keywords, exclude_numbered):
                            continue
                        if set_keyword not in title.lower() or len(title) < 40:
                            _, _, total = parse_total_cost(item, default_shipping)
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
                        st.info("No poorly titled or missing-specifics listings flagged for this parallel.")

        except Exception as e:
            st.error(f"eBay API Error: {e}")
