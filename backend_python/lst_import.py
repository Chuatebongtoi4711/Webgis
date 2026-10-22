"""
Nạp dữ liệu chỉ số theo PHƯỜNG/XÃ x THÁNG vào bảng du_lieu_chi_so.

Quy ước của backend (xem main.py): mỗi dòng dữ liệu là 1 bản ghi ma_chi_so = 'LST',
gia_tri = LST (°C); NDVI và TVDI nằm trong cột jsonb bien_phu_giai_thich
({"ndvi": ..., "tvdi": ...}). Vị trí điểm lấy bằng ST_PointOnSurface(ranh_gioi)
của phường/xã nên luôn nằm TRONG ranh giới (khác với tâm hình học, có thể rơi ra ngoài).

Mã phường/xã trong tệp phải trùng khu_vuc.ma_hanh_chinh (vd. 25747 = MA_DVHC).
"""
import io
import json
import math
from typing import Dict, Optional

import pandas as pd
from psycopg2.extras import Json, execute_values

# Tên cột chấp nhận được (không phân biệt hoa/thường) -> tên chuẩn
COLUMN_ALIASES = {
    "ward_code": ["ward_code", "ma_hanh_chinh", "ma_dvhc", "ma_xa", "ma_phuong", "maphuong"],
    "thoi_gian": ["thoi_gian", "thangdau", "thang_dau", "date", "month_start", "ngay"],
    "lst": ["lst", "lst_mean", "gia_tri_lst"],
    "ndvi": ["ndvi", "ndvi_mean"],
    "tvdi": ["tvdi", "tvdi_mean"],
    "year": ["year", "nam"],
    "month": ["month", "thang"],
    # Tùy chọn: biến giải thích thêm cho các mô hình AI
    "luong_mua": ["luong_mua", "rain", "precip"],
    "do_am_dat": ["do_am_dat", "soil_moisture"],
    "lop_phu_be_mat": ["lop_phu_be_mat", "landcover"],
}
LST_RANGE = (10.0, 65.0)   # khớp danh_muc_chi_so
NDVI_RANGE = (-1.0, 1.0)
TVDI_RANGE = (0.0, 1.0)


class ImportError_(ValueError):
    """Lỗi dữ liệu đầu vào (thông báo tiếng Việt, hiển thị thẳng cho người dùng)."""


def read_csv_bytes(data: bytes) -> pd.DataFrame:
    """Đọc CSV (UTF-8 có/không BOM, hoặc cp1258/latin-1), mọi cột đọc dạng chuỗi."""
    last = None
    for enc in ("utf-8-sig", "cp1258", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(data), dtype=str, encoding=enc, sep=None, engine="python")
        except UnicodeDecodeError as e:
            last = e
        except Exception as e:  # CSV hỏng cấu trúc
            raise ImportError_(f"Không đọc được tệp CSV: {e}") from e
    raise ImportError_(f"Không xác định được bảng mã của tệp CSV: {last}")


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.strip().str.replace(",", ".", regex=False), errors="coerce")


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Đổi tên cột về tên chuẩn, ép kiểu; ném ImportError_ nếu thiếu cột bắt buộc."""
    lower = {str(c).strip().lower(): c for c in df.columns}
    out = pd.DataFrame(index=df.index)
    for std, names in COLUMN_ALIASES.items():
        for n in names:
            if n in lower:
                out[std] = df[lower[n]]
                break

    missing = [c for c in ("ward_code", "lst") if c not in out.columns]
    if "thoi_gian" not in out.columns and not {"year", "month"} <= set(out.columns):
        missing.append("thoi_gian (hoặc cặp year + month)")
    if missing:
        raise ImportError_(
            "Tệp thiếu cột bắt buộc: " + ", ".join(missing)
            + f". Các cột hiện có: {', '.join(map(str, df.columns))}."
        )

    out["ward_code"] = out["ward_code"].astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
    if "thoi_gian" in out.columns:
        out["thoi_gian"] = pd.to_datetime(out["thoi_gian"], errors="coerce").dt.date
    else:
        y, m = _num(out["year"]), _num(out["month"])
        out["thoi_gian"] = [
            pd.Timestamp(int(a), int(b), 1).date() if pd.notna(a) and pd.notna(b) and 1 <= b <= 12 else pd.NaT
            for a, b in zip(y, m)
        ]
    for c in ("lst", "ndvi", "tvdi", "luong_mua", "do_am_dat"):
        if c in out.columns:
            out[c] = _num(out[c])
    return out


def _clean(v, lo, hi):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return float(v) if lo <= float(v) <= hi else None


def ensure_dataset(conn, ma_dataset: int, ten: str, nguon: str, duong_dan: Optional[str], ma_nguoi_dung=None) -> int:
    """Trả về ma_dataset đã có; nếu chưa có thì tạo mới (id do DB cấp) và trả id mới."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM dataset WHERE ma_dataset = %s", (ma_dataset,))
        if cur.fetchone():
            if duong_dan:
                cur.execute("UPDATE dataset SET duong_dan_file = %s WHERE ma_dataset = %s", (duong_dan[:255], ma_dataset))
            return ma_dataset
        cur.execute(
            """INSERT INTO dataset (ma_nguoi_dung, ten_dataset, nguon_du_lieu, duong_dan_file, ngay_tao)
               VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING ma_dataset""",
            (ma_nguoi_dung, ten[:150], nguon[:50], (duong_dan or "")[:255] or None),
        )
        return cur.fetchone()[0]


def import_dataframe(conn, df: pd.DataFrame, ma_dataset: int, loai_nguon: str = "GEE_ACTUAL") -> Dict:
    """
    Ghi dữ liệu vào du_lieu_chi_so (KHÔNG commit - người gọi tự commit/rollback).
    Dòng trùng (dataset, phường/xã, ngày, LST, loại nguồn) với dữ liệu cũ sẽ được ghi đè.
    """
    rows_in_file = len(df)
    d = normalize(df)

    skipped = {"thieu_ngay": 0, "thieu_lst": 0, "lst_ngoai_khoang": 0, "ma_khong_khop": 0,
               "thieu_ranh_gioi": 0, "trung_lap_trong_tep": 0}

    bad_date = d["thoi_gian"].isna()
    skipped["thieu_ngay"] = int(bad_date.sum())
    d = d[~bad_date]

    no_lst = d["lst"].isna()
    skipped["thieu_lst"] = int(no_lst.sum())
    d = d[~no_lst]

    out_rng = (d["lst"] < LST_RANGE[0]) | (d["lst"] > LST_RANGE[1])
    skipped["lst_ngoai_khoang"] = int(out_rng.sum())
    d = d[~out_rng]

    with conn.cursor() as cur:
        cur.execute("SELECT ma_hanh_chinh, ma_khu_vuc FROM khu_vuc "
                    "WHERE loai_khu_vuc <> 'THANH_PHO' AND ma_hanh_chinh IS NOT NULL")
        code_to_id = {str(k).strip(): v for k, v in cur.fetchall()}

    known = d["ward_code"].isin(code_to_id)
    unmatched = sorted(d.loc[~known, "ward_code"].unique().tolist())
    skipped["ma_khong_khop"] = int((~known).sum())
    d = d[known]

    before = len(d)
    d = d.drop_duplicates(subset=["ward_code", "thoi_gian"], keep="last")
    skipped["trung_lap_trong_tep"] = before - len(d)

    if d.empty:
        raise ImportError_(
            "Không có dòng nào hợp lệ để nạp. "
            f"Mã phường/xã không khớp khu_vuc.ma_hanh_chinh: {', '.join(unmatched[:10]) or '(không có)'}; "
            f"bỏ qua: {skipped}."
        )

    payload = []
    for r in d.itertuples(index=False):
        extra = {}
        ndvi = _clean(getattr(r, "ndvi", None), *NDVI_RANGE)
        tvdi = _clean(getattr(r, "tvdi", None), *TVDI_RANGE)
        if ndvi is not None:
            extra["ndvi"] = round(ndvi, 4)
        if tvdi is not None:
            extra["tvdi"] = round(tvdi, 4)
        for k in ("luong_mua", "do_am_dat"):
            v = getattr(r, k, None)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                extra[k] = float(v)
        lp = getattr(r, "lop_phu_be_mat", None)
        if isinstance(lp, str) and lp.strip():
            extra["lop_phu_be_mat"] = lp.strip()
        payload.append((code_to_id[r.ward_code], r.thoi_gian, float(r.lst), Json(extra)))

    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _imp (ma_khu_vuc bigint, thoi_gian date, gia_tri double precision, bv jsonb) "
                    "ON COMMIT DROP")
        execute_values(cur, "INSERT INTO _imp VALUES %s", payload, page_size=2000)

        cur.execute(
            """DELETE FROM du_lieu_chi_so f USING _imp i
               WHERE f.ma_dataset = %s AND f.ma_khu_vuc = i.ma_khu_vuc AND f.thoi_gian = i.thoi_gian
                 AND f.ma_chi_so = 'LST' AND f.loai_nguon_du_lieu = %s""",
            (ma_dataset, loai_nguon),
        )
        replaced = cur.rowcount

        cur.execute(
            """INSERT INTO du_lieu_chi_so
                   (ma_dataset, ma_khu_vuc, thoi_gian, vi_do, kinh_do, ma_chi_so, gia_tri,
                    loai_nguon_du_lieu, bien_phu_giai_thich)
               SELECT %s, i.ma_khu_vuc, i.thoi_gian, ST_Y(p.g), ST_X(p.g), 'LST', i.gia_tri, %s, i.bv
               FROM _imp i
               JOIN LATERAL (SELECT ST_PointOnSurface(k.ranh_gioi) AS g
                             FROM khu_vuc k WHERE k.ma_khu_vuc = i.ma_khu_vuc) p ON p.g IS NOT NULL""",
            (ma_dataset, loai_nguon),
        )
        inserted = cur.rowcount
        skipped["thieu_ranh_gioi"] = len(payload) - inserted

        cur.execute("SELECT MIN(thoi_gian), MAX(thoi_gian) FROM _imp")
        d0, d1 = cur.fetchone()
        cur.execute(
            """UPDATE dataset SET
                   thoi_gian_bat_dau = LEAST(COALESCE(thoi_gian_bat_dau, %s), %s),
                   thoi_gian_ket_thuc = GREATEST(COALESCE(thoi_gian_ket_thuc, %s), %s)
               WHERE ma_dataset = %s""",
            (d0, d0, d1, d1, ma_dataset),
        )

    n_wards = d["ward_code"].nunique()
    n_months = d["thoi_gian"].nunique()
    report = {
        "ma_dataset": ma_dataset,
        "so_dong_trong_tep": rows_in_file,
        "so_dong_da_nap": inserted,
        "so_dong_ghi_de": replaced,
        "so_phuong_xa": int(n_wards),
        "so_moc_thoi_gian": int(n_months),
        "tu_ngay": str(d0),
        "den_ngay": str(d1),
        "bo_qua": skipped,
        "ma_khong_khop_mau": unmatched[:20],
    }
    report["message"] = (
        f"Đã nạp {inserted} dòng cho {n_wards} phường/xã ({n_months} mốc thời gian, {d0} → {d1}) "
        f"vào bộ dữ liệu #{ma_dataset}."
        + (f" Ghi đè {replaced} dòng cũ." if replaced else "")
        + (f" Bỏ qua {sum(skipped.values())} dòng (xem chi tiết bo_qua)." if sum(skipped.values()) else "")
    )
    return report