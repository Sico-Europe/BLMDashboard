from io import StringIO
import pandas as pd
import numpy as np
import streamlit as st
import dropbox
import re
#import datetime
from zoneinfo import ZoneInfo
#import math

#Workday assumptions
work_days = 4   # Mon-Thu: weekday() 0..3
workday_offset = pd.offsets.CustomBusinessDay(weekmask="Mon Tue Wed Thu")
shift_start = "07:00"
shift_end = "17:00"
breaks = [("10:00", "10:15"), ("13:00", "13:30")]

file = "/blmbender.txt"

def now():
    return pd.Timestamp.now(tz=ZoneInfo("Europe/London")).tz_localize(None)

@st.cache_data(ttl=300)
def data_handling():
    text = download_data(file) 
    if not text:
        st.error("No data returned from Dropbox download.")
        return None
    df = load_log(text)
    today = runs_today(df)
    weekly = split_weekly(df)
    part_breakdown = weekly_part_counts(df)
    tube_breakdown = df.groupby("tube_type", as_index=False).agg(Parts=("no_parts", "sum"))
    return today, weekly, part_breakdown, tube_breakdown

def download_data(file):
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=st.secrets["DROPBOX_REFRESH_TOKEN"],
        app_key=st.secrets["DROPBOX_APP_KEY"],
        app_secret=st.secrets["DROPBOX_APP_SECRET"],)
    metadata, response = dbx.files_download(file)

    return response.content.decode("utf-8")

def part_key(names):
    """'280134-01CROPPED' and '280134-01Toptubes' -> '280134-01'"""
    return names.fillna("").str.extract(r"^\s*([A-Za-z]*\d+(?:-\d+)?)")[0].str.lower()

def runs_this_week(runs): #Filter this week only
    today = pd.Timestamp.today().normalize()
    week_start = today - pd.Timedelta(days=today.weekday())
    week_end = week_start + pd.Timedelta(days=6)
    this_week_runs = runs[(runs.Date >= week_start) & (runs.Date <= week_end)]
    return this_week_runs

def load_log(text):
    headers = ["Finish","col1","no_parts","sum_cycle_time","start","Program","col6","shape","y_length","x_length","material","no_straight_sections","real_cut_length","theoretical_cut_length","wall_thickness","col15","theoretical_bend_time"]
    df = pd.read_csv(StringIO(text), sep=";", names=headers, header=None, index_col=False)
    df = df.drop(columns=["col1","col6","x_length","material","theoretical_cut_length","wall_thickness","col15"])
    df = df.drop_duplicates()
    
    
    datetime_columns = ["Finish", "start"]
    for col in datetime_columns:
        df[col] = pd.to_datetime(df[col],format="%Y/%m/%d %H:%M:%S",errors="coerce")
    
    df = current_row(df)

    df = split_by_day(df)
    df["Program"] = part_key(df["Program"])
    df["Date"] = df["Finish"].dt.normalize()

    df = df.sort_values("start", kind="stable").reset_index(drop=True)

    df = df.sort_values("start", kind="stable").reset_index(drop=True)

    df["tube_type"] = np.select(
        [df["shape"] == 3, df["y_length"] == 25.4, df["y_length"] == 31.75, df["y_length"] == 41.275],
        ["1 in square", "1 in round", "1 1/4 in round", "1 5/8 in round"],
        default="other")
    df["Edited"] = (df["Program"] == df["Program"].shift()) & (df["Date"] == df["Date"].shift())
    df["Changeover"] = df["shape"] != df["shape"].shift()

    df["total_time"] = (df["Finish"] - df["start"]).dt.total_seconds()
        

    df["total_time"] = (df["Finish"] - df["start"]).dt.total_seconds()
    df["bend_time"]=df["theoretical_bend_time"]*df["no_parts"]
    df["load_time"]=df["sum_cycle_time"]-df["bend_time"]
    
    df = per_part_columns(df)
    df = perfomance(df)
    
    return df.sort_values("start", kind="stable").reset_index(drop=True)

def weekly_part_counts(df):
    df = df.copy()
    df["Week"] = df["Finish"].dt.to_period("W").dt.start_time
    return df.groupby(["Week", "Program"], as_index=False).agg(Count=("no_parts", "sum"))

def split_weekly(runs): #data per week
    runs = runs.copy()
    
    runs["Week"] = runs["Date"].dt.to_period("W").dt.start_time
    runs = runs.loc[runs["no_parts"]> 0]
    weekly = runs.groupby("Week").agg(
        Days_Recorded=("Date", "nunique"), #how many days this week did the machine run
        Total_Runs=("Program", "count"),
        Total_Parts=("no_parts", "sum"),
        Total_Runtime=("sum_cycle_time", "sum"),
        Total_Bending_Time=("bend_time", "sum"),
        Total_Loading_Time=("load_time", "sum"),
        Edits=("Edited", "sum"),
    ).reset_index()


    valid = runs[runs["Performance"].notna() & (runs["no_parts"] > 0)]

    weighted = (valid["Performance"] * valid["no_parts"]).groupby(valid["Week"]).sum()
    total = valid["no_parts"].groupby(valid["Week"]).sum()

    weekly["Performance"] = weekly["Week"].map(weighted / total)

    # shift and break time available so far in each week
    current = now()
    shifts, week_breaks = [], []
    for week in weekly["Week"]:
        days = [week + pd.DateOffset(days=d) for d in range(work_days)]
        shifts.append(sum(real_shift(d, current) for d in days))
        week_breaks.append(sum(real_breaks(d, current) for d in days))
    weekly["Shift_Time"] = shifts
    weekly["Breaks_Time"] = week_breaks

    planned = (weekly["Shift_Time"] - weekly["Breaks_Time"]).where(lambda p: p > 0)
    busy = weekly["Total_Bending_Time"] + weekly["Total_Loading_Time"]
    weekly["Total_Downtime"] = (planned.fillna(0) - busy).clip(lower=0)
    weekly["Availability"] = (busy / planned).clip(upper=1)

    return weekly

def real_breaks(day, current):
    total = 0

    for bstart, bend in breaks:
        b0 = pd.Timestamp(
            f"{day:%Y-%m-%d} {bstart}"
        )
        b1 = pd.Timestamp(
            f"{day:%Y-%m-%d} {bend}"
        )

        end = min(b1, current)

        if end > b0:
            total += (end - b0).total_seconds()

    return total


def real_shift(day, current):
    if day.weekday() >= work_days:
        return 0.0

    s = pd.Timestamp(
        f"{day:%Y-%m-%d} {shift_start}"
    )
    e = pd.Timestamp(
        f"{day:%Y-%m-%d} {shift_end}"
    )

    return max(0.0, (min(e, current) - s).total_seconds())


def split_by_day(df, split_cols=("no_parts", "sum_cycle_time")):
    df = df.copy()
    df["row_id"] = df.index

    # One entry per calendar day
    df["day"] = [
        list(pd.date_range(s.normalize(), e.normalize(), freq="D"))
        for s, e in zip(df["start"], df["Finish"])
    ]

    df = df.explode("day").reset_index(drop=True)
    df["day"] = pd.to_datetime(df["day"])

    next_midnight = df["day"] + pd.Timedelta(days=1)

    # Actual segment boundaries
    seg_start = df["start"].where(
        df["start"] > df["day"], df["day"]
    )

    seg_end = df["Finish"].where(
        df["Finish"] < next_midnight, next_midnight
    )

    # Calculate actual time spent on each day
    full_secs = (df["Finish"] - df["start"]).dt.total_seconds()
    seg_secs = (seg_end - seg_start).dt.total_seconds()

    share = (seg_secs / full_secs).where(full_secs > 0, 1.0)

    # Remove empty segments
    keep = (seg_secs > 0) | (full_secs == 0)

    # Distribute production proportionally
    for col in split_cols:
        df[col] = df[col] * share

    # Set start and end to the desired daily boundaries
    df["start"] = seg_start
    df["Finish"] = seg_end.where(
        seg_end != next_midnight,
        next_midnight - pd.Timedelta(minutes=1)
    )

    return df.loc[keep].drop(
        columns=["day"]
    ).reset_index(drop=True)

def current_row(df):
    df = df.copy()
    for idx, row in df.iterrows():
        if row["no_parts"] == 999999999:
            df.at[idx, "Finish"] = now()
            df.at[idx, "no_parts"] = None
            df.at[idx, "sum_cycle_time"] = None

    return df

def per_part_columns(df):
    df = df.copy()
    parts = df["no_parts"].where(df["no_parts"] > 0)
    df["bend_time_per_part"] = df["bend_time"] / parts
    df["load_time_per_part"] = df["load_time"] / parts
    df["cycle_time_per_part"] = df["sum_cycle_time"] / parts
    return df

def runs_today(runs): #Filter today only
    today = pd.Timestamp.today().normalize()
    today_runs = runs[(runs["Finish"].dt.normalize() == today) &(runs["no_parts"].notna())]
    return today_runs

def perfomance(df):
    df = df.copy()
    lower = prod(df).set_index("Program")["Lower_Quartile"]
    df["Performance"] = (df["Program"].map(lower) * df["no_parts"]) / df["total_time"]
    return df

def prod(runs): #calculate mean, lower and upper quartiles of run time for each product  
    if runs.empty:
        return runs

    prod_data = ( #Group by product
        runs
        .groupby("Program", as_index=False)
        .agg(
            Mean_Time_Per_Part=("cycle_time_per_part", "median"),
            Lower_Quartile=("cycle_time_per_part", lambda x: x.quantile(0.25)),
            Upper_Quartile=("cycle_time_per_part", lambda x: x.quantile(0.75)),
            Run_Count=("no_parts", "sum")
        )
    )
    prod_data["Confidence"] = prod_data["Run_Count"] > 200 
    return prod_data

#today,weekly,part_breakdown = data_handling()
#print(weekly)






"""







#def summarise(file):
    #rows = []
    #for line in text.splitlines():
        #m = LINE_RE.match(line)
        #if m:
            #rows.append(m.groupdict())
    #df = pd.DataFrame(rows).drop(columns=["Type"])
    #df["DateTime"] = pd.to_datetime(df["DateTime"], format="%Y-%m-%d %H:%M:%S,%f")

    #return program, count
    #and week, part count, availability, performance
    #save to Dropbox as CSV

#end,,no_parts,cycle_time,start,part,,shape,y_lenght,x_length,material,straight_sections,real_cut_length,theoretical_cut_length,wall_thickness,,theoretical_cycle_time?
#ongoing part: first 4:9999/99/99 99:99:99;999999999;999999999;999999999;



def build_runs(df):
    program_start = re.compile(r"Auto Cycle started partnumber\s+(?P<partnumber>\S+)")
    program_end = re.compile(r"Auto Cycle Complete Cycle Time\s+(?P<cycle_time>[\d.]+)")
    edit_start_row = re.compile(r"Open Form Part Editor - Editing Part: (?P<partnumber>\S+)\s+Units: (?P<units>\S+)")
    edit_end = re.compile(r"Close Form Part Editor - Editing Part: (?P<partnumber>\S+)\s+Units: (?P<units>\S+)")

    runs = []
    active_start = {}
    active_partnumber = None
    edit_start = {}

    for DateTime, Event in df[["DateTime", "Event"]].itertuples(index=False):
        
        m_start = program_start.match(Event)
        m_end = program_end.match(Event)
        m_edit_start = edit_start_row.match(Event)
        m_edit_end = edit_end.match(Event)

        if m_start:
            active_start[m_start.group("partnumber")] = DateTime
            active_partnumber = m_start.group("partnumber")
        elif m_end:
            if active_partnumber != None:
                runs.append({
                    "Program": active_partnumber,
                    "Start": active_start[active_partnumber],
                    "Finish": DateTime,
                    "Event": "Bent part",
                    "Cycle_Time": float(m_end.group("cycle_time")),
                })
            active_partnumber = None
        elif m_edit_start:
            edit_start[m_edit_start.group("partnumber")] = DateTime
        elif m_edit_end:
            if m_edit_end.group("partnumber") in edit_start:
                runs.append({
                    "Program": m_edit_end.group("partnumber"),
                    "Start": edit_start[m_edit_end.group("partnumber")],
                    "Finish": DateTime,
                    "Event": "Edited part",
                    "Cycle_Time": None,
                })
                del edit_start[m_edit_end.group("partnumber")]

    runs = pd.DataFrame(runs)
    if runs.empty:
        return runs

    runs["Cycle_Time"] = pd.to_numeric(runs["Cycle_Time"])
    runs["Total_time"] = (runs["Finish"] - runs["Start"]).dt.total_seconds()
    runs["Extra_Time"] = runs["Total_time"] - runs["Cycle_Time"]                                             
    return runs

def real_breaks(day, current):
    total = 0
    for bstart, bend in breaks:  # Have the breaks happened yet?
        b0 = pd.Timestamp(f"{day:%Y-%m-%d} {bstart}")
        b1 = pd.Timestamp(f"{day:%Y-%m-%d} {bend}")
        end = min(b1, current)          # for past days this is the full break
        if end > b0:
            total += (end - b0).total_seconds()
    return total

def real_shift(day, current):
    if day.weekday() >= work_days:
        return 0.0
    s = pd.Timestamp(f"{day:%Y-%m-%d} {shift_start}")
    e = pd.Timestamp(f"{day:%Y-%m-%d} {shift_end}")
    return max(0.0, (min(e, current) - s).total_seconds())

def loading_prediction(runs):
    stats = prod(runs)
    runs["Downtime"] = np.nan
    runs["Loading"] = np.nan
    runs["Performance"] = np.nan 
    for idx, row in runs.iterrows():
        runs.at[idx, "Downtime"] = 0.0
        if row.Event == "Bent part":
            prod_stats = stats[stats["Program"] == row.Program]
            if not prod_stats.empty:
                mean = prod_stats["Mean_Time_Per_Part"].iloc[0]
                upper = prod_stats["Upper_Quartile"].iloc[0]
                lower = prod_stats["Lower_Quartile"].iloc[0]
                confidence = prod_stats["Confidence"].iloc[0]
                if confidence:
                    runs.at[idx, "Performance"] = lower / row.Total_time
                    if row.Total_time > upper:
                        runs.at[idx, "Downtime"] = max(0, row["Total_time"] - mean)
            runs.at[idx, "Loading"] = row["Total_time"] - row ["Cycle_Time"]- runs.at[idx, "Downtime"]
    return runs

def split_weekly(runs):
    #Add in stored summary data
    #Currently returns: Week, Days Recorded, Total Runs, Total Bending Time, Total Loading Time, Total Editing Time, Shift Time, Breaks Time, Total Downtime, Availability
    runs["Week"] = runs["Finish"].dt.to_period("W").dt.start_time
    loading = loading_prediction(runs)

    split = loading.groupby("Week").agg(
        Days_Recorded=("Finish", lambda x: x.dt.normalize().nunique()),   # was "nunique" (counts timestamps)
        Total_Runs=("Program", "count"),
        Total_Bending_Time=("Cycle_Time", "sum"),
        Total_Loading_Time=("Loading", "sum"),
        Performance=("Performance", "mean"),
    )
    edit_time = loading[loading["Event"] == "Edited part"].groupby("Week")["Total_time"].sum()
    split["Total_Editing_Time"] = edit_time
    split["Total_Editing_Time"] = split["Total_Editing_Time"].fillna(0)

    # shift and break time available so far in each week
    current = now()
    shifts, week_breaks = [], []
    for week in split.index:
        days = [week + pd.Timedelta(days=d) for d in range(work_days)]
        days = [d for d in days if d >= start_date]
        shifts.append(sum(real_shift(d, current) for d in days))
        week_breaks.append(sum(real_breaks(d, current) for d in days))
    split["Shift_Time"] = shifts
    split["Breaks_Time"] = week_breaks

    planned = (split["Shift_Time"] - split["Breaks_Time"]).where(lambda p: p > 0)
    busy = split["Total_Bending_Time"] + split["Total_Loading_Time"] + split["Total_Editing_Time"]
    split["Total_Downtime"] = (planned.fillna(0) - busy).clip(lower=0)
    split["Availability"] = (busy / planned).clip(upper=1)
    return split
      
def prod(runs): #calculate mean, lower and upper quartiles of run time for each product  
    if runs.empty:
        return runs

    prod_data = (
        runs[runs["Event"] == "Bent part"]
        .groupby("Program", as_index=False)
        .agg(
            Mean_Time_Per_Part=("Total_time", "mean"),
            Lower_Quartile=("Total_time", lambda x: x.quantile(0.25)),
            Upper_Quartile=("Total_time", lambda x: x.quantile(0.75)),
            Run_count =("Program", "count"),

        )
    )
    prod_data["Confidence"] = prod_data["Run_count"] > 200 
    return prod_data



def upload_data(data, path):

    csv_data = data.to_csv(index=False).encode("utf-8")

    dbx = dropbox.Dropbox(
        oauth2_refresh_token=st.secrets["DROPBOX_REFRESH_TOKEN"],
        app_key=st.secrets["DROPBOX_APP_KEY"],
        app_secret=st.secrets["DROPBOX_APP_SECRET"],
    )

    dbx.files_upload(
        csv_data,
        path,
        mode=dropbox.files.WriteMode.overwrite,
    )



def productive_seconds_per_day():
    start_dt = datetime.datetime.strptime(shift_start, "%H:%M")
    end_dt = datetime.datetime.strptime(shift_end, "%H:%M")
    shift_seconds = (end_dt - start_dt).total_seconds()

    break_seconds = sum(
        (datetime.datetime.strptime(bend, "%H:%M") - datetime.datetime.strptime(bstart, "%H:%M")).total_seconds()
        for bstart, bend in breaks
    )
    return shift_seconds - break_seconds - (warmup_time * 60) - (cleandown_time * 60)


def predict_job_time(avg_cycle_time, avg_changeover_time, qty):
    routing_time = avg_cycle_time * qty
    available_per_day = productive_seconds_per_day() - avg_changeover_time

    if available_per_day <= 0:
        # changeover alone eats the whole day's productive time - degenerate case
        changeovers = max(1, math.ceil(routing_time / max(avg_changeover_time, 1)))
    else:
        changeovers = max(1, math.ceil(routing_time / available_per_day))

    total_time = routing_time + changeovers * avg_changeover_time
    return total_time, changeovers

def format_duration(seconds):

    minutes = seconds / 60
    hours = seconds / 3600

    shift_start_dt = datetime.datetime.strptime(shift_start, "%H:%M")
    shift_end_dt = datetime.datetime.strptime(shift_end, "%H:%M")
    shift_hours = productive_seconds_per_day() / 3600

    if minutes < 60:
        return f"{minutes:.0f} min"
    elif hours < shift_hours:
        return f"{hours:.1f} hours"
    else:
        shifts = hours / shift_hours
        return f"{hours:.1f} hours (~{shifts:.1f} shifts)"

def compute_job_list(input_df, summary):
    rows = []
    grand_total_seconds = 0

    for _, r in input_df.iterrows():
        program, qty = r.get("Program"), r.get("Number Required")

        if pd.isna(program) or pd.isna(qty) or program not in summary["Program"].values:
            rows.append({
                "Program": program, "Number Required": qty,
                "Estimated Batches": None, "Changeover Time": "",
                "Routing Time": "", "Total Time": "",
            })
            continue

        qty = int(qty)
        stat = summary[summary["Program"] == program].iloc[0]
        total_seconds, changeovers = predict_job_time(stat["Avg_Cycle_Time"], stat["Avg_Changeover_Time"], qty)
        routing_seconds = stat["Avg_Cycle_Time"] * qty
        changeover_seconds = total_seconds - routing_seconds
        grand_total_seconds += total_seconds

        rows.append({
            "Program": program, "Number Required": qty,
            "Estimated Batches": changeovers,
            "Changeover Time": format_duration(changeover_seconds),
            "Routing Time": format_duration(routing_seconds),
            "Total Time": format_duration(total_seconds),
        })

    columns = ["Program", "Number Required", "Estimated Batches", "Changeover Time", "Routing Time", "Total Time"]
    return pd.DataFrame(rows, columns=columns), grand_total_seconds

"""