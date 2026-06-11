import time

def add_meta(data, query_start_time, data_version=None, load_source="postgres"):
    query_ms = (time.time() - query_start_time) * 1000
    meta = {
        "query_ms": round(query_ms, 2),
        "load_source": load_source,
        "api_version": "v1"
    }
    if data_version:
        meta["data_version"] = data_version
    return {"data": data, "meta": meta}