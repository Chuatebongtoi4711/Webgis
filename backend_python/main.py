"""
FastAPI Backend - Hệ thống WebGIS AI Dự báo LST TP.HCM
Kết nối trực tiếp CSDL PostgreSQL/PostGIS `lst_hcm` (v8_Final, 10 bảng):
  nguoi_dung, khu_vuc, danh_muc_chi_so, dataset, tien_xu_ly,
  model, training_run, model_metric, du_lieu_chi_so, thong_ke_va_lop_ban_do

Quy ước dữ liệu trong bảng du_lieu_chi_so (bảng trung tâm):
  - ma_chi_so = 'LST' (hoặc 'NDVI', 'TVDI'); gia_tri = giá trị chỉ số
  - loai_nguon_du_lieu: GEE_ACTUAL | GEE_INTERPOLATED (hiện trạng), AI_FORECAST (dự báo), SIMULATION (giả lập)
  - bien_phu_giai_thich (jsonb) chứa biến giải thích tại cùng điểm/ngày với các khóa:
        ndvi, tvdi, luong_mua, do_am_dat, lop_phu_be_mat
JSON trả về giữ nguyên tên trường cũ để giao diện React không phải sửa.
"""

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import os
import json
import math
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import bcrypt
import jwt
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db
from models_ai import run_model_training
from preprocessor import LSTPreprocessor

app = FastAPI(
    title="Hệ thống WebGIS AI Dự báo LST - TP.HCM (API Service)",
    description="REST API đọc/ghi CSDL PostgreSQL/PostGIS lst_hcm: dữ liệu LST, tiền xử lý, huấn luyện 4 mô hình AI và bản đồ WebGIS",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JWT_SECRET = os.getenv("JWT_SECRET", "webgis_lst_hcm_ai_secret_key_2026")

# Điều kiện lọc dữ liệu LST hiện trạng (alias bảng: f)
LST_OBS = "f.ma_chi_so = 'LST' AND f.loai_nguon_du_lieu IN ('GEE_ACTUAL', 'GEE_INTERPOLATED')"


def _bv(key: str) -> str:
    """Lấy một biến giải thích dạng số từ jsonb."""
    return f"(f.bien_phu_giai_thich->>'{key}')::double precision"


# Dạng "bảng rộng" quen thuộc cho giao diện và module AI
LST_WIDE_COLUMNS = f"""
    f.ma_ban_ghi AS ma_lst, f.ma_dataset,
    to_char(f.thoi_gian, 'YYYY-MM-DD') AS thoi_gian,
    f.vi_do, f.kinh_do, f.gia_tri AS gia_tri_lst,
    {_bv('ndvi')} AS ndvi,
    {_bv('luong_mua')} AS luong_mua,
    {_bv('do_am_dat')} AS do_am_dat,
    f.bien_phu_giai_thich->>'lop_phu_be_mat' AS lop_phu_be_mat
"""


def _f(x):
    """Ép về float an toàn (NaN/None -> None) trước khi ghi DB."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _resolve_khu_vuc(code: Optional[str]) -> Optional[int]:
    """
    Đổi mã khu vực gửi từ giao diện sang ma_khu_vuc (bigint) của DB.
    - 'HCM' hoặc 'HCMC'  -> dòng THANH_PHO
    - mã hành chính (vd '27316') -> theo cột ma_hanh_chinh
    - số nguyên -> theo ma_khu_vuc
    """
    if not code:
        return None
    c = str(code).strip()
    if c.upper() in ("HCM", "HCMC"):
        rows = db.query("SELECT ma_khu_vuc FROM khu_vuc WHERE loai_khu_vuc = 'THANH_PHO' ORDER BY ma_khu_vuc LIMIT 1")
    else:
        rows = db.query("SELECT ma_khu_vuc FROM khu_vuc WHERE ma_hanh_chinh = %s", (c,))
        if not rows and c.isdigit():
            rows = db.query("SELECT ma_khu_vuc FROM khu_vuc WHERE ma_khu_vuc = %s", (int(c),))
    return rows[0]["ma_khu_vuc"] if rows else None


def _is_city(code: Optional[str]) -> bool:
    return bool(code) and str(code).strip().upper() in ("HCM", "HCMC")


def _load_lst_df(ma_dataset: int) -> pd.DataFrame:
    rows = db.query(
        f"SELECT {LST_WIDE_COLUMNS} FROM du_lieu_chi_so f WHERE {LST_OBS} AND f.ma_dataset = %s",
        (ma_dataset,),
    )
    return pd.DataFrame(rows)


def _admin_id() -> Optional[int]:
    rows = db.query("SELECT ma_nguoi_dung FROM nguoi_dung WHERE vai_tro = 'ADMIN' AND trang_thai = 'HOAT_DONG' ORDER BY ma_nguoi_dung LIMIT 1")
    return rows[0]["ma_nguoi_dung"] if rows else None


# ==========================================
# PYDANTIC SCHEMAS
# ==========================================
class LoginRequest(BaseModel):
    ten_dang_nhap: str
    mat_khau: str


class TrainingRequest(BaseModel):
    ma_dataset: int = 1
    ma_model: int
    cach_xu_ly_thieu: str = "Noi_suy_tuyen_tinh"
    cach_xu_ly_nhieu: str = "IQR"
    phuong_phap_chuan_hoa: str = "Min-Max"
    tham_so: Optional[Dict[str, Any]] = None


# ==========================================
# 0. KIỂM TRA HỆ THỐNG
# ==========================================
@app.get("/api/health", tags=["Hệ thống"])
def health():
    """Số dòng của 10 bảng - dùng để kiểm tra web đang đọc đúng CSDL."""
    tables = ["nguoi_dung", "khu_vuc", "danh_muc_chi_so", "dataset", "tien_xu_ly",
              "model", "training_run", "model_metric", "du_lieu_chi_so", "thong_ke_va_lop_ban_do"]
    counts = {t: db.query(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"] for t in tables}
    info = db.query("SELECT current_database() AS db, version() AS version")[0]
    return {"status": "ok", "database": info["db"], "postgres": info["version"].split(",")[0], "row_counts": counts}


# ==========================================
# 1. XÁC THỰC & NGƯỜI DÙNG (NGUOI_DUNG)
# ==========================================
@app.post("/api/auth/login", tags=["Xác thực & Người dùng"])
def login(req: LoginRequest):
    users = db.query(
        "SELECT ma_nguoi_dung, ten_dang_nhap, mat_khau_ma_hoa, email, vai_tro, trang_thai "
        "FROM nguoi_dung WHERE ten_dang_nhap = %s",
        (req.ten_dang_nhap,),
    )
    invalid = HTTPException(status_code=401, detail="Tên đăng nhập hoặc mật khẩu không chính xác")
    if not users:
        raise invalid
    user = users[0]

    try:
        ok = bcrypt.checkpw(req.mat_khau.encode("utf-8"), user["mat_khau_ma_hoa"].encode("utf-8"))
    except Exception:
        ok = False
    if not ok:
        raise invalid
    if user.get("trang_thai") == "KHOA":
        raise HTTPException(status_code=403, detail="Tài khoản đã bị khóa")

    token = jwt.encode(
        {
            "ma_nguoi_dung": user["ma_nguoi_dung"],
            "vai_tro": user["vai_tro"],
            "ten_dang_nhap": user["ten_dang_nhap"],
            "exp": datetime.utcnow() + timedelta(hours=24),
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return {
        "token": token,
        "user": {
            "ma_nguoi_dung": user["ma_nguoi_dung"],
            "ten_dang_nhap": user["ten_dang_nhap"],
            "email": user["email"],
            "vai_tro": user["vai_tro"],
        },
    }


@app.get("/api/users", tags=["Xác thực & Người dùng"])
def get_users():
    return db.query("SELECT ma_nguoi_dung, ten_dang_nhap, email, vai_tro, trang_thai FROM nguoi_dung ORDER BY ma_nguoi_dung")


# ==========================================
# 2. KHU VỰC HÀNH CHÍNH & RANH GIỚI (KHU_VUC)
# ==========================================
@app.get("/api/khu-vuc", tags=["Khu vực & Ranh giới WebGIS"])
def get_khu_vuc(
    loai: Optional[str] = Query(None, description="THANH_PHO | PHUONG_XA"),
    cha: Optional[str] = Query(None, description="Mã khu vực cha, vd 'HCM' để lấy 168 phường/xã"),
    geometry: bool = Query(False, description="true: kèm ranh giới GeoJSON"),
):
    """
    Trả về ma_khu_vuc = mã hành chính (chuỗi) để giao diện dùng làm mã khu vực.
    Trường `id` là khóa số thật trong DB.
    """
    geo = ", ST_AsGeoJSON(kv.ranh_gioi, 5) AS geojson" if geometry else ""
    where, params = [], []
    if loai:
        loai_db = "PHUONG_XA" if loai.upper() in ("PHUONG", "XA", "DAC_KHU") else loai
        where.append("kv.loai_khu_vuc = %s")
        params.append(loai_db)
    if cha:
        cha_id = _resolve_khu_vuc(cha)
        if cha_id is None:
            return []
        where.append("kv.ma_khu_vuc_cha = %s")
        params.append(cha_id)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.query(
        f"""
        SELECT kv.ma_khu_vuc AS id, kv.ma_hanh_chinh AS ma_khu_vuc, kv.ma_hanh_chinh,
               kv.ten_khu_vuc, kv.loai_khu_vuc, p.ma_hanh_chinh AS ma_khu_vuc_cha,
               kv.dien_tich_km2{geo}
        FROM khu_vuc kv
        LEFT JOIN khu_vuc p ON p.ma_khu_vuc = kv.ma_khu_vuc_cha
        {where_sql}
        ORDER BY (kv.ma_khu_vuc_cha IS NOT NULL), kv.ten_khu_vuc
        """,
        tuple(params),
    )
    if geometry:
        for r in rows:
            g = r.get("geojson")
            try:
                r["geojson"] = json.loads(g) if isinstance(g, str) else g
            except Exception:
                r["geojson"] = None
    return rows


# ==========================================
# 3. BỘ DỮ LIỆU (DATASET)
# ==========================================
@app.get("/api/datasets", tags=["Bộ dữ liệu LST"])
def get_datasets():
    return db.query(
        f"""
        SELECT d.*, u.ten_dang_nhap AS nguoi_tao,
               (SELECT COUNT(*) FROM du_lieu_chi_so f WHERE {LST_OBS} AND f.ma_dataset = d.ma_dataset) AS so_ban_ghi,
               (SELECT ROUND(AVG(f.gia_tri)::numeric, 2) FROM du_lieu_chi_so f WHERE {LST_OBS} AND f.ma_dataset = d.ma_dataset) AS lst_trung_binh,
               (SELECT ROUND(MAX(f.gia_tri)::numeric, 2) FROM du_lieu_chi_so f WHERE {LST_OBS} AND f.ma_dataset = d.ma_dataset) AS lst_cao_nhat,
               (SELECT ROUND(MIN(f.gia_tri)::numeric, 2) FROM du_lieu_chi_so f WHERE {LST_OBS} AND f.ma_dataset = d.ma_dataset) AS lst_thap_nhat
        FROM dataset d
        LEFT JOIN nguoi_dung u ON d.ma_nguoi_dung = u.ma_nguoi_dung
        ORDER BY d.ma_dataset
        """
    )


# ==========================================
# 4. DỮ LIỆU LST (du_lieu_chi_so, ma_chi_so = 'LST')
# ==========================================
@app.get("/api/lst-data", tags=["Dữ liệu LST"])
def get_lst_data(
    ma_dataset: int = 1,
    thoi_gian: Optional[str] = None,
    tu_ngay: Optional[str] = None,
    den_ngay: Optional[str] = None,
    limit: int = 1000,
):
    sql = f"SELECT {LST_WIDE_COLUMNS} FROM du_lieu_chi_so f WHERE {LST_OBS} AND f.ma_dataset = %s"
    params = [ma_dataset]
    if thoi_gian:
        sql += " AND f.thoi_gian = %s::date"
        params.append(thoi_gian)
    if tu_ngay:
        sql += " AND f.thoi_gian >= %s::date"
        params.append(tu_ngay)
    if den_ngay:
        sql += " AND f.thoi_gian <= %s::date"
        params.append(den_ngay)
    sql += " ORDER BY f.thoi_gian DESC, f.ma_ban_ghi ASC LIMIT %s"
    params.append(max(1, min(limit, 20000)))
    try:
        return db.query(sql, tuple(params))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Tham số ngày không hợp lệ hoặc lỗi truy vấn: {e}")


@app.get("/api/lst-data/timeline", tags=["Dữ liệu LST"])
def get_lst_timeline(ma_dataset: int = 1):
    """Các ngày quan sát + LST trung bình toàn TP.HCM, dùng cho thanh trượt thời gian."""
    return db.query(
        f"""
        SELECT to_char(f.thoi_gian, 'YYYY-MM-DD') AS thoi_gian,
               ROUND(AVG(f.gia_tri)::numeric, 2) AS avg_lst,
               ROUND(MAX(f.gia_tri)::numeric, 2) AS max_lst,
               ROUND(MIN(f.gia_tri)::numeric, 2) AS min_lst,
               ROUND(AVG({_bv('ndvi')})::numeric, 3) AS avg_ndvi,
               COUNT(*) AS points_count
        FROM du_lieu_chi_so f
        WHERE {LST_OBS} AND f.ma_dataset = %s
        GROUP BY f.thoi_gian
        ORDER BY f.thoi_gian ASC
        """,
        (ma_dataset,),
    )


# ==========================================
# 5. TIỀN XỬ LÝ (TIEN_XU_LY)
# ==========================================
@app.get("/api/tien-xu-ly", tags=["Tiền xử lý dữ liệu"])
def get_tien_xu_ly():
    return db.query("SELECT * FROM tien_xu_ly ORDER BY ma_tien_xu_ly DESC")


@app.post("/api/tien-xu-ly/pipeline", tags=["Tiền xử lý dữ liệu"])
def run_preprocessing_pipeline(
    ma_dataset: int = 1,
    cach_xu_ly_thieu: str = "interpolate",
    cach_xu_ly_nhieu: str = "iqr",
    phuong_phap_chuan_hoa: str = "min_max",
):
    df = _load_lst_df(ma_dataset)
    if df.empty:
        raise HTTPException(status_code=404, detail="Chưa có dữ liệu LST quan trắc cho dataset này trong bảng du_lieu_chi_so")

    prep = LSTPreprocessor(
        missing_strategy="interpolate" if ("noi_suy" in cach_xu_ly_thieu.lower() or "interpolat" in cach_xu_ly_thieu.lower()) else "drop",
        outlier_strategy="iqr" if "iqr" in cach_xu_ly_nhieu.lower() else "z_score",
        norm_method="min_max" if ("min-max" in phuong_phap_chuan_hoa.lower() or "min_max" in phuong_phap_chuan_hoa.lower()) else "z_score",
    )
    df_clean, miss_stats = prep.handle_missing(df)
    df_clean, outlier_stats = prep.handle_outliers(df_clean)
    train_df, val_df, test_df = prep.chronological_split(df_clean)
    train_df, val_df, test_df, scaler_info = prep.fit_normalize(train_df, val_df, test_df)
    n = max(len(df_clean), 1)
    return {
        "status": "success",
        "missing_stats": miss_stats,
        "outlier_stats": outlier_stats,
        "scaler_info": scaler_info,
        "data_split": {
            "total_records": len(df_clean),
            "train_count": len(train_df),
            "val_count": len(val_df),
            "test_count": len(test_df),
            "split_ratio": f"{round(len(train_df)/n*100)}% / {round(len(val_df)/n*100)}% / {round(len(test_df)/n*100)}%",
        },
    }


# ==========================================
# 6. MÔ HÌNH AI (MODEL, TRAINING_RUN, MODEL_METRIC)
# ==========================================
# Giao diện dùng nhãn cũ 'Kiem_dinh'/'Kiem_tra'; DB lưu 'VALIDATION'/'TEST'.
TAP_LABEL = "CASE mm.tap_danh_gia WHEN 'TEST' THEN 'Kiem_tra' ELSE 'Kiem_dinh' END"


@app.get("/api/models", tags=["Mô hình AI"])
def get_models():
    return db.query("SELECT * FROM model ORDER BY ma_model")


@app.get("/api/training-runs", tags=["Huấn luyện & Đánh giá"])
def get_training_runs():
    return db.query(
        """
        SELECT tr.*, m.ten_model, m.loai_model, d.ten_dataset,
               m_val.mae AS val_mae, m_val.rmse AS val_rmse, m_val.r2 AS val_r2, m_val.mape AS val_mape,
               m_test.mae AS test_mae, m_test.rmse AS test_rmse, m_test.r2 AS test_r2, m_test.mape AS test_mape
        FROM training_run tr
        JOIN model m ON tr.ma_model = m.ma_model
        LEFT JOIN dataset d ON tr.ma_dataset = d.ma_dataset
        LEFT JOIN model_metric m_val ON tr.ma_training = m_val.ma_training AND m_val.tap_danh_gia = 'VALIDATION'
        LEFT JOIN model_metric m_test ON tr.ma_training = m_test.ma_training AND m_test.tap_danh_gia = 'TEST'
        ORDER BY tr.ma_training DESC
        """
    )


@app.post("/api/training/run", tags=["Huấn luyện & Đánh giá"])
def trigger_training(req: TrainingRequest):
    """Huấn luyện mô hình đã chọn trên LST quan trắc, ghi training_run + model_metric (VALIDATION, TEST)."""
    model_row = db.query("SELECT * FROM model WHERE ma_model = %s", (req.ma_model,))
    if not model_row:
        raise HTTPException(status_code=404, detail="Không tìm thấy mô hình tương ứng")
    model_name = model_row[0]["ten_model"]

    df = _load_lst_df(req.ma_dataset)
    if df.empty or len(df) < 15:
        raise HTTPException(
            status_code=400,
            detail="Không đủ dữ liệu LST để huấn luyện: bảng du_lieu_chi_so chưa có (hoặc quá ít) dữ liệu LST quan trắc cho dataset này. Hãy nạp dữ liệu từ GEE trước.",
        )

    txl_rows = db.query(
        "SELECT * FROM tien_xu_ly WHERE ma_dataset = %s ORDER BY ma_tien_xu_ly DESC LIMIT 1", (req.ma_dataset,)
    )
    txl = txl_rows[0] if txl_rows else {}
    ma_tien_xu_ly = txl.get("ma_tien_xu_ly")
    train_end_date = str(txl.get("ngay_ket_thuc_train") or "2022-12-31")
    val_end_date = str(txl.get("ngay_ket_thuc_val") or "2023-12-31")

    prep = LSTPreprocessor(train_end_date=train_end_date, val_end_date=val_end_date)
    df_clean, _ = prep.handle_missing(df)
    df_clean, _ = prep.handle_outliers(df_clean)

    hyper_params = req.tham_so or {}
    start_time = datetime.now()
    try:
        train_res = run_model_training(
            model_name, df_clean, hyper_params,
            train_end_date=train_end_date, val_end_date=val_end_date,
        )
        val_m = train_res["val_metrics"]
        test_m = train_res["test_metrics"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi huấn luyện mô hình: {str(e)}")
    end_time = datetime.now()

    run_id = db.execute_insert(
        """
        INSERT INTO training_run (ma_dataset, ma_tien_xu_ly, ma_model, ma_nguoi_dung, ma_chi_so_muc_tieu,
                                  tham_so, trang_thai, thoi_gian_bat_dau, thoi_gian_ket_thuc)
        VALUES (%s, %s, %s, %s, 'LST', %s::jsonb, 'HOAN_THANH', %s, %s)
        RETURNING ma_training
        """,
        (req.ma_dataset, ma_tien_xu_ly, req.ma_model, _admin_id(), json.dumps(hyper_params), start_time, end_time),
    )

    for tap, m in (("VALIDATION", val_m), ("TEST", test_m)):
        db.query(
            "INSERT INTO model_metric (ma_training, tap_danh_gia, mae, rmse, r2, mape) VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, tap, _f(m["mae"]), _f(m["rmse"]), _f(m["r2"]), _f(m["mape"])),
        )

    return {
        "status": "success",
        "ma_training": run_id,
        "model": model_name,
        "val_metrics": val_m,
        "test_metrics": test_m,
        "execution_seconds": round((end_time - start_time).total_seconds(), 2),
        "details": train_res.get("details") or train_res.get("feature_importances"),
    }


@app.get("/api/metrics/comparison", tags=["Huấn luyện & Đánh giá"])
def get_metrics_comparison():
    """Bảng so sánh 4 chỉ số MAE, RMSE, R², MAPE giữa các mô hình."""
    return db.query(
        f"""
        SELECT m.ten_model, m.loai_model,
               {TAP_LABEL} AS tap_danh_gia,
               ROUND(AVG(mm.mae)::numeric, 3) AS mae,
               ROUND(AVG(mm.rmse)::numeric, 3) AS rmse,
               ROUND(AVG(mm.r2)::numeric, 3) AS r2,
               ROUND(AVG(mm.mape)::numeric, 2) AS mape
        FROM model_metric mm
        JOIN training_run tr ON mm.ma_training = tr.ma_training
        JOIN model m ON tr.ma_model = m.ma_model
        GROUP BY m.ten_model, m.loai_model, mm.tap_danh_gia
        ORDER BY m.ten_model, (mm.tap_danh_gia = 'TEST')
        """
    )


# ==========================================
# 7. DỰ BÁO & LỚP BẢN ĐỒ (du_lieu_chi_so AI_FORECAST, thong_ke_va_lop_ban_do)
# ==========================================
@app.get("/api/forecast", tags=["Dự báo LST & Bản đồ WebGIS"])
def get_forecast(
    ma_training: Optional[int] = None,
    ma_khu_vuc: Optional[str] = None,
    thoi_gian: Optional[str] = None,
    limit: Optional[int] = None,
):
    sql = """
        SELECT f.ma_ban_ghi AS ma_du_bao, f.ma_training,
               kv.ma_hanh_chinh AS ma_khu_vuc,
               to_char(f.thoi_gian, 'YYYY-MM-DD') AS thoi_gian_du_bao,
               f.vi_do, f.kinh_do, f.gia_tri AS gia_tri_lst_du_bao,
               kv.ten_khu_vuc, m.ten_model
        FROM du_lieu_chi_so f
        LEFT JOIN khu_vuc kv ON f.ma_khu_vuc = kv.ma_khu_vuc
        LEFT JOIN training_run tr ON f.ma_training = tr.ma_training
        LEFT JOIN model m ON tr.ma_model = m.ma_model
        WHERE f.ma_chi_so = 'LST' AND f.loai_nguon_du_lieu = 'AI_FORECAST'
    """
    params = []
    if ma_training:
        sql += " AND f.ma_training = %s"
        params.append(ma_training)
    if ma_khu_vuc and not _is_city(ma_khu_vuc):
        kv_id = _resolve_khu_vuc(ma_khu_vuc)
        if kv_id is None:
            return []
        sql += " AND (f.ma_khu_vuc = %s OR kv.ma_khu_vuc_cha = %s)"
        params.extend([kv_id, kv_id])
    if thoi_gian:
        sql += " AND f.thoi_gian = %s::date"
        params.append(thoi_gian)
    sql += " ORDER BY f.thoi_gian ASC, f.ma_ban_ghi ASC"
    if limit:
        sql += " LIMIT %s"
        params.append(max(1, limit))
    return db.query(sql, tuple(params))


@app.get("/api/layers", tags=["Dự báo LST & Bản đồ WebGIS"])
def get_map_layers():
    return db.query(
        """
        SELECT t.ma_thong_ke_layer AS ma_layer, t.ma_training,
               kv.ma_hanh_chinh AS ma_khu_vuc, kv.ten_khu_vuc,
               t.ma_chi_so, t.loai_nguon_du_lieu,
               (t.ma_chi_so || ' - ' || COALESCE(kv.ten_khu_vuc, '') || ' - ' || t.thoi_gian::text) AS ten_layer,
               t.thoi_gian, t.gia_tri_min, t.gia_tri_max, t.gia_tri_trung_binh,
               t.do_lech_chuan, t.dien_tich_nguy_co_km2, t.muc_do_canh_bao,
               t.duong_dan_ban_do AS duong_dan_du_lieu, t.dinh_dang_ban_do AS loai_du_lieu,
               m.ten_model
        FROM thong_ke_va_lop_ban_do t
        LEFT JOIN khu_vuc kv ON t.ma_khu_vuc = kv.ma_khu_vuc
        LEFT JOIN training_run tr ON t.ma_training = tr.ma_training
        LEFT JOIN model m ON tr.ma_model = m.ma_model
        ORDER BY t.ma_thong_ke_layer DESC
        """
    )


# ==========================================
# 8. THỐNG KÊ TỔNG QUAN
# ==========================================
@app.get("/api/overview-stats", tags=["Thống kê"])
def get_overview_stats():
    def count(sql):
        return db.query(sql)[0]["c"]

    cnt_ds = count("SELECT COUNT(*) AS c FROM dataset")
    cnt_lst = count(f"SELECT COUNT(*) AS c FROM du_lieu_chi_so f WHERE {LST_OBS}")
    cnt_models = count("SELECT COUNT(*) AS c FROM model")
    cnt_runs = count("SELECT COUNT(*) AS c FROM training_run")
    cnt_fc = count("SELECT COUNT(*) AS c FROM du_lieu_chi_so f WHERE f.ma_chi_so = 'LST' AND f.loai_nguon_du_lieu = 'AI_FORECAST'")

    best = db.query(
        """
        SELECT m.ten_model, mm.rmse, mm.r2, mm.mae, mm.mape
        FROM model_metric mm
        JOIN training_run tr ON mm.ma_training = tr.ma_training
        JOIN model m ON tr.ma_model = m.ma_model
        WHERE mm.tap_danh_gia = 'TEST' AND mm.rmse IS NOT NULL
        ORDER BY mm.rmse ASC
        LIMIT 1
        """
    )

    span = db.query(f"SELECT MIN(f.thoi_gian) AS d1, MAX(f.thoi_gian) AS d2 FROM du_lieu_chi_so f WHERE {LST_OBS}")[0]
    if span["d1"] is None:
        span = db.query("SELECT MIN(thoi_gian_bat_dau) AS d1, MAX(thoi_gian_ket_thuc) AS d2 FROM dataset")[0]
    time_span = f"{span['d1'].year} - {span['d2'].year}" if span["d1"] and span["d2"] else "Chưa có dữ liệu"

    return {
        "datasets_count": cnt_ds,
        "lst_records_count": cnt_lst,
        "models_count": cnt_models,
        "training_runs_count": cnt_runs,
        "forecast_points_count": cnt_fc,
        "best_model": best[0] if best else None,
        "time_span": time_span,
        "city_focus": "TP. Hồ Chí Minh",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
