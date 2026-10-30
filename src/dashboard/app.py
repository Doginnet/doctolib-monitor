"""Streamlit HealthTech Command Center: Clinic Geo-Map, Slot Sniper & GKV vs. PKV Wait-Time Analytics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from src.alerts.dispatcher import AlertDispatcher
from src.pipelines.storage import DoctolibStorage


st.set_page_config(
    page_title="Doctolib Germany — Slot Sniper & Wait-Time Intelligence",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .sniper-card {
        background: linear-gradient(145deg, #131c31 0%, #0f172a 100%);
        border: 1px solid #1e293b;
        border-left: 4px solid #10b981;
        border-radius: 12px;
        padding: 16px 18px;
        margin-bottom: 14px;
    }
    .sniper-card.moderate { border-left-color: #f59e0b; }
    .sniper-card.long { border-left-color: #ef4444; }
    .badge-gkv {
        background-color: rgba(59, 130, 246, 0.18);
        color: #60a5fa;
        border: 1px solid rgba(59, 130, 246, 0.4);
        padding: 2px 9px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .badge-pkv {
        background-color: rgba(234, 179, 8, 0.18);
        color: #facc15;
        border: 1px solid rgba(234, 179, 8, 0.4);
        padding: 2px 9px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .badge-wait {
        background-color: rgba(16, 185, 129, 0.18);
        color: #34d399;
        padding: 3px 10px;
        border-radius: 8px;
        font-weight: 700;
        font-size: 0.85rem;
    }
    .book-btn {
        display: inline-block;
        margin-top: 8px;
        padding: 6px 14px;
        background-color: #0284c7;
        color: #ffffff !important;
        text-decoration: none;
        border-radius: 7px;
        font-size: 0.84rem;
        font-weight: 600;
    }
    .book-btn:hover { background-color: #0369a1; }
    </style>
    """,
    unsafe_allow_html=True,
)


def load_data(active_only: bool) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    db_path = settings.resolved_db_path()
    storage = DoctolibStorage(db_path)
    records = storage.get_latest_availability_records(active_only=active_only, limit=5000)
    gap_rows = storage.get_insurance_gap_summary(active_only=active_only)
    intents = storage.get_booking_intents(limit=100)
    return pd.DataFrame(records), pd.DataFrame(gap_rows), intents


def assign_pin_color(wait_days: float | None) -> list[int]:
    if wait_days is None or pd.isna(wait_days):
        return [148, 163, 184, 160]  # Slate: no online slot
    if wait_days <= 3.0:
        return [16, 185, 129, 230]  # Emerald: Urgent 0-3d
    if wait_days <= 14.0:
        return [245, 158, 11, 220]  # Amber: 4-14d
    return [239, 68, 68, 210]  # Crimson: 15d+ bottleneck


def main() -> None:
    st.title("🩺 Doctolib Germany — Real-Time Slot Sniper & Wait-Time Monitor")
    st.caption(
        "Multi-Stage REST API Intelligence (`doctolib.de`) • GKV (Gesetzlich) vs. PKV (Privat) Access Gap • "
        "Pluggable Dry-Run Booking & Webhook Engine"
    )

    with st.sidebar:
        st.header("🎛️ Sniper & Filter Controls")
        active_only = st.checkbox("Active practitioners only (is_active = 1)", value=True)

    df, gap_df, intents = load_data(active_only=active_only)

    if df.empty:
        st.warning(
            "No availability snapshots found in `data/doctolib.db`. "
            "Run `uv run python -m src.main --city berlin --specialty kardiologie --insurance both --auto-book-mock` first."
        )
        return

    with st.sidebar:
        cities = sorted(df["city_name"].dropna().unique().tolist())
        selected_cities = st.multiselect("Cities", options=cities, default=cities)

        specialties = sorted(df["specialty_name"].dropna().unique().tolist())
        selected_specs = st.multiselect("Specialties", options=specialties, default=specialties)

        sector_filter = st.radio(
            "Insurance Sector",
            options=["All", "public (GKV - Gesetzlich)", "private (PKV - Privat)"],
            index=0,
        )

        max_wait_slider = st.slider(
            "Sniper Wait Threshold (Days)",
            min_value=1.0,
            max_value=90.0,
            value=14.0,
            step=1.0,
        )
        include_unbookable = st.checkbox("Include clinics with no open slots", value=True)
        telehealth_only = st.checkbox("Video-consultation (Telehealth) only", value=False)
        search_text = st.text_input("Search Doctor / Street / Zip", value="").strip().lower()

    filtered = df.copy()
    if selected_cities:
        filtered = filtered[filtered["city_name"].isin(selected_cities)]
    if selected_specs:
        filtered = filtered[filtered["specialty_name"].isin(selected_specs)]
    if sector_filter.startswith("public"):
        filtered = filtered[filtered["insurance_sector"] == "public"]
    elif sector_filter.startswith("private"):
        filtered = filtered[filtered["insurance_sector"] == "private"]
    if telehealth_only:
        filtered = filtered[filtered["telehealth"] == 1]
    if not include_unbookable:
        filtered = filtered[filtered["wait_time_days"].notna()]
    if search_text:
        mask = (
            filtered["full_name"].fillna("").str.lower().str.contains(search_text)
            | filtered["street_address"].fillna("").str.lower().str.contains(search_text)
            | filtered["zipcode"].fillna("").str.lower().str.contains(search_text)
        )
        filtered = filtered[mask]

    urgent_df = filtered[
        filtered["wait_time_days"].notna() & (filtered["wait_time_days"] <= max_wait_slider)
    ].sort_values("wait_time_days", ascending=True)

    gkv_sub = filtered[
        (filtered["insurance_sector"] == "public") & filtered["wait_time_days"].notna()
    ]
    pkv_sub = filtered[
        (filtered["insurance_sector"] == "private") & filtered["wait_time_days"].notna()
    ]
    gkv_avg = float(gkv_sub["wait_time_days"].mean()) if not gkv_sub.empty else None
    pkv_avg = float(pkv_sub["wait_time_days"].mean()) if not pkv_sub.empty else None

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Monitored Calendars", f"{len(filtered):,}")
    k2.metric(f"Urgent Slots (<= {max_wait_slider:.0f}d)", f"{len(urgent_df):,}")
    k3.metric("GKV (Public) Avg Wait", f"{gkv_avg:.1f} days" if gkv_avg is not None else "N/A")
    gap_delta = (
        f"{(gkv_avg / pkv_avg):.1f}x faster than GKV"
        if (gkv_avg and pkv_avg and pkv_avg > 0)
        else None
    )
    k4.metric(
        "PKV (Private) Avg Wait",
        f"{pkv_avg:.1f} days" if pkv_avg is not None else "N/A",
        delta=gap_delta,
    )

    tab_map, tab_sniper, tab_analytics, tab_automation, tab_data = st.tabs(
        [
            "🗺️ Clinic Availability Geo-Map",
            "🎯 Appointment Sniper Feed",
            "⚖️ GKV vs. PKV Wait-Time Analytics",
            "🤖 Dry-Run Auto-Booking & Webhooks",
            "📋 B2B Clinic Directory & Export",
        ]
    )

    with tab_map:
        st.subheader("Geo-Coded Clinic Availability Map")
        st.caption(
            "🟢 **0–3 days** (Immediate / Cancellation) • 🟡 **4–14 days** (Moderate) • "
            "🔴 **15+ days** (Bottleneck) • ⚪ **No online slots**"
        )
        map_df = filtered.dropna(subset=["latitude", "longitude"]).copy()
        if map_df.empty:
            st.info("No clinics with GPS coordinates match the current filter.")
        else:
            map_df["color"] = map_df["wait_time_days"].apply(assign_pin_color)
            map_df["wait_label"] = map_df["wait_time_days"].apply(
                lambda x: f"{x:.1f} days" if pd.notna(x) else "No open online slots"
            )
            map_df["slot_label"] = (
                map_df["next_slot_iso"].fillna("—").str.slice(0, 16).str.replace("T", " ")
            )
            map_df["sector_label"] = map_df["insurance_sector"].str.upper()
            map_df["phone_label"] = map_df["landline_number"].fillna("Not enriched")

            mid_lat = float(map_df["latitude"].median())
            mid_lng = float(map_df["longitude"].median())

            layer = pdk.Layer(
                "ScatterplotLayer",
                data=map_df,
                get_position=["longitude", "latitude"],
                get_fill_color="color",
                get_radius=190,
                radius_min_pixels=6,
                radius_max_pixels=18,
                pickable=True,
                stroked=True,
                get_line_color=[15, 23, 42, 240],
                line_width_min_pixels=1,
            )
            view_state = pdk.ViewState(
                latitude=mid_lat,
                longitude=mid_lng,
                zoom=11.0 if len(selected_cities) == 1 else 6.0,
                pitch=25,
            )
            deck = pdk.Deck(
                layers=[layer],
                initial_view_state=view_state,
                map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
                tooltip={
                    "html": (
                        "<b>{full_name}</b><br/>"
                        "Specialty: {specialty_name} ({sector_label})<br/>"
                        "Wait Time: <b>{wait_label}</b><br/>"
                        "Earliest Slot: {slot_label}<br/>"
                        "Address: {street_address}, {zipcode} {city_name}<br/>"
                        "Tel: {phone_label}"
                    ),
                    "style": {
                        "backgroundColor": "#0f172a",
                        "color": "#f8fafc",
                        "fontSize": "12px",
                        "border": "1px solid #334155",
                    },
                },
            )
            st.pydeck_chart(deck, use_container_width=True)

    with tab_sniper:
        st.subheader(f"Urgent Bookable Slots (<= {max_wait_slider:.0f} Days)")
        if urgent_df.empty:
            st.info("No open slots within the selected wait-time threshold. Increase the slider.")
        else:
            cols = st.columns(2)
            for idx, (_, row) in enumerate(urgent_df.head(30).iterrows()):
                wait_d = float(row["wait_time_days"])
                card_cls = "" if wait_d <= 3.0 else ("moderate" if wait_d <= 14.0 else "long")
                sec_cls = "badge-gkv" if row["insurance_sector"] == "public" else "badge-pkv"
                sec_txt = (
                    "GKV • Gesetzlich"
                    if row["insurance_sector"] == "public"
                    else "PKV • Privat"
                )
                slot_fmt = str(row["next_slot_iso"] or "")[:16].replace("T", " ")
                langs = ", ".join(json.loads(row["languages_json"] or "[]")).upper() or "DE"
                raw_phone = row.get("landline_number")
                has_phone = pd.notna(raw_phone) and bool(str(raw_phone).strip())
                phone_html = (
                    f" &nbsp;|&nbsp; 📞 <code>{raw_phone}</code>"
                    if has_phone
                    else ""
                )
                street_str = str(row["street_address"]) if pd.notna(row.get("street_address")) else ""
                zip_str = str(row["zipcode"]) if pd.notna(row.get("zipcode")) else ""
                city_str = str(row["city_name"]) if pd.notna(row.get("city_name")) else ""
                addr = f"{street_str}, {zip_str} {city_str}".strip(", ")
                with cols[idx % 2]:
                    st.markdown(
                        f"""
                        <div class="sniper-card {card_cls}">
                            <div style="display:flex; justify-content:space-between; align-items:center;">
                                <span class="{sec_cls}">{sec_txt}</span>
                                <span class="badge-wait">⚡ {wait_d:.1f}d wait ({int(row['open_slots_count'])} slots/15d)</span>
                            </div>
                            <h4 style="margin:10px 0 4px 0; color:#f8fafc;">{row['full_name']}</h4>
                            <div style="color:#94a3b8; font-size:0.88rem; margin-bottom:6px;">
                                {row['specialty_name']} • 🌐 {langs}{phone_html}
                            </div>
                            <div style="color:#e2e8f0; font-size:0.9rem;">
                                📅 <b>Earliest Termin:</b> <code>{slot_fmt}</code> ({row.get('motive_name') or 'Sprechstunde'})<br/>
                                📍 {addr}
                            </div>
                            <a class="book-btn" href="{row['profile_url']}" target="_blank">Book on Doctolib ↗</a>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

    with tab_analytics:
        st.subheader("Public (GKV) vs. Private (PKV) Healthcare Access Gap")
        c_left, c_right = st.columns(2)

        with c_left:
            if not gap_df.empty:
                chart_df = gap_df.dropna(subset=["avg_wait_days"]).copy()
                chart_df["Scope"] = chart_df["city_name"] + " — " + chart_df["specialty_name"]
                chart_df["Insurance"] = chart_df["insurance_sector"].map(
                    {"public": "GKV (Public)", "private": "PKV (Private)"}
                )
                bar = (
                    alt.Chart(chart_df)
                    .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5)
                    .encode(
                        x=alt.X(
                            "Scope:N",
                            title="City & Specialty",
                            sort="-y",
                            axis=alt.Axis(labelAngle=-25, labelLimit=220),
                        ),
                        y=alt.Y("avg_wait_days:Q", title="Average Wait Time (Days)"),
                        color=alt.Color(
                            "Insurance:N",
                            scale=alt.Scale(
                                domain=["GKV (Public)", "PKV (Private)"],
                                range=["#3b82f6", "#eab308"],
                            ),
                        ),
                        xOffset="Insurance:N",
                        tooltip=[
                            "city_name",
                            "specialty_name",
                            "Insurance",
                            "avg_wait_days",
                            "min_wait_days",
                            "with_open_slots",
                        ],
                    )
                    .properties(height=360, title="Average Wait Time (Days): GKV vs. PKV")
                )
                st.altair_chart(bar, use_container_width=True)

        with c_right:
            bookable_df = filtered.dropna(subset=["wait_time_days"]).copy()
            if not bookable_df.empty:
                bucket_order = [
                    "0–3d (Urgent)",
                    "4–7d (1 Week)",
                    "8–14d (2 Weeks)",
                    "15–30d (1 Month)",
                    "30d+ (Long)",
                ]
                bookable_df["Wait Bucket"] = pd.cut(
                    bookable_df["wait_time_days"],
                    bins=[-0.1, 3, 7, 14, 30, 999],
                    labels=bucket_order,
                )
                bookable_df["Insurance"] = bookable_df["insurance_sector"].map(
                    {"public": "GKV (Public)", "private": "PKV (Private)"}
                )
                dist = (
                    alt.Chart(bookable_df)
                    .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                    .encode(
                        x=alt.X(
                            "Wait Bucket:N",
                            title="Wait-Time Distribution Bucket",
                            sort=bucket_order,
                            axis=alt.Axis(labelAngle=-25),
                        ),
                        y=alt.Y("count():Q", title="Number of Practitioners"),
                        color=alt.Color(
                            "Insurance:N",
                            scale=alt.Scale(
                                domain=["GKV (Public)", "PKV (Private)"],
                                range=["#3b82f6", "#eab308"],
                            ),
                        ),
                        tooltip=["Wait Bucket", "Insurance", "count()"],
                    )
                    .properties(height=360, title="Appointment Lead-Time Distribution")
                )
                st.altair_chart(dist, use_container_width=True)

    with tab_automation:
        dispatcher = AlertDispatcher()
        mode_str = "🟢 Safe Mock / Simulation Mode" if dispatcher.is_mock_mode else "🚀 Live Webhook Delivery"
        st.subheader(f"Dry-Run Auto-Booking & Webhook Dispatcher ({mode_str})")
        st.info(
            "To prevent spamming real medical clinics on `doctolib.de`, reservations execute in **Dry-Run Mock Mode**, "
            "enriching clinic phone numbers (`landline_number`) and emitting structured JSON events ready for **n8n**, **Slack**, or **Telegram**."
        )
        if not intents:
            st.write("No mock booking intents recorded yet.")
        else:
            intents_df = pd.DataFrame(intents)
            st.dataframe(
                intents_df[
                    [
                        "id",
                        "doctor_name",
                        "specialty",
                        "city",
                        "insurance_sector",
                        "target_slot_iso",
                        "wait_time_days",
                        "clinic_phone",
                        "status",
                        "created_at",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )
            st.markdown("##### Latest Standardized n8n / Webhook Payload (`BookingIntentPayload`)")
            st.json(json.loads(intents[0]["payload_json"]))

    with tab_data:
        st.subheader("Full B2B Clinic & Availability Explorer")
        export_cols = [
            "full_name",
            "specialty_name",
            "city_name",
            "insurance_sector",
            "wait_time_days",
            "next_slot_iso",
            "open_slots_count",
            "landline_number",
            "street_address",
            "zipcode",
            "latitude",
            "longitude",
            "is_active",
            "profile_url",
        ]
        present_cols = [c for c in export_cols if c in filtered.columns]
        st.dataframe(filtered[present_cols], use_container_width=True, hide_index=True)

        dl1, dl2 = st.columns(2)
        dl1.download_button(
            "⬇️ Download Filtered CSV",
            data=filtered[present_cols].to_csv(index=False).encode("utf-8"),
            file_name="doctolib_availability_export.csv",
            mime="text/csv",
        )
        dl2.download_button(
            "⬇️ Download Filtered JSON",
            data=filtered[present_cols].to_json(orient="records", force_ascii=False, indent=2),
            file_name="doctolib_availability_export.json",
            mime="application/json",
        )


if __name__ == "__main__":
    main()
