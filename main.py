import datetime

def process_audit_event(input_ws, raw_audit_text, script_url):
    # 1. Clear previous missing roster entries in P6:P37
    safe_sheet_action(input_ws.batch_clear, ["P6:P37"])

    # 2. Extract Audit Date from line 2 of raw audit text for validation
    lines = [line.strip() for line in raw_audit_text.split("\n") if line.strip()]
    audit_date_str = None
    if len(lines) >= 2:
        date_line = lines[1].replace("Date:", "").replace("date:", "").strip()
        # Look for standard DD/MM/YYYY format
        import re
        match = re.search(r"\d{2}/\d{2}/\d{4}", date_line)
        if match:
            audit_date_str = match.group(0)

    # 3. Check Weekly Date Boundaries in J3 (Start Date) and K3 (End Date)
    if audit_date_str:
        start_date_val = safe_sheet_action(input_ws.acell, "J3").value
        end_date_val = safe_sheet_action(input_ws.acell, "K3").value
        
        try:
            audit_dt = datetime.datetime.strptime(audit_date_str, "%d/%m/%Y")
            if start_date_val and end_date_val:
                start_dt = datetime.datetime.strptime(str(start_date_val).strip(), "%d/%m/%Y")
                end_dt = datetime.datetime.strptime(str(end_date_val).strip(), "%d/%m/%Y")
                
                if not (start_dt <= audit_dt <= end_dt):
                    return {
                        "status": "error",
                        "message": f"⚠️ Audit date (`{audit_date_str}`) falls outside the active week boundary (`{start_date_val}` to `{end_date_val}`)."
                    }
        except ValueError:
            pass  # Fall through if date formatting fails to parse strictly

    # 4. Paste raw audit text into C3
    safe_sheet_action(input_ws.update_acell, "C3", raw_audit_text)

    # 5. Trigger Google Apps Script Web App
    response = requests.post(script_url, json={"action": "run"}, timeout=45)
    
    if response.status_code != 200:
        return {"status": "error", "message": "Failed to communicate with Google Apps Script."}

    # 6. Read Missing Roster entries populated by Apps Script in P6:P37
    missing_vals = safe_sheet_action(input_ws.get, "P6:P37")
    missing_players = []
    if missing_vals:
        for row in missing_vals:
            if row and len(row) > 0 and str(row[0]).strip():
                missing_players.append(str(row[0]).strip())

    return {
        "status": "success",
        "missing_players": missing_players
    }
