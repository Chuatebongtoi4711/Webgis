-- =====================================================================
-- HỆ THỐNG WEBGIS TÍCH HỢP AI DỰ BÁO NHIỆT ĐỘ BỀ MẶT ĐẤT (LST) - TP.HCM
-- CSDL PostgreSQL (+ PostGIS) - bám theo tài liệu phân tích thiết kế
-- 10 thực thể: NGUOI_DUNG, DATASET, KHU_VUC, LST_DATA, TIEN_XU_LY, 
--              MODEL, TRAINING_RUN, MODEL_METRIC, FORECAST_RESULT, MAP_LAYER
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

DROP TABLE IF EXISTS map_layer       CASCADE;
DROP TABLE IF EXISTS forecast_result CASCADE;
DROP TABLE IF EXISTS model_metric    CASCADE;
DROP TABLE IF EXISTS training_run    CASCADE;
DROP TABLE IF EXISTS model           CASCADE;
DROP TABLE IF EXISTS tien_xu_ly      CASCADE;
DROP TABLE IF EXISTS lst_data        CASCADE;
DROP TABLE IF EXISTS dataset         CASCADE;
DROP TABLE IF EXISTS khu_vuc         CASCADE;
DROP TABLE IF EXISTS nguoi_dung      CASCADE;

-- 1. NGUOI_DUNG
CREATE TABLE nguoi_dung (
    ma_nguoi_dung    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ten_dang_nhap    VARCHAR(50)  NOT NULL UNIQUE,
    mat_khau_ma_hoa  VARCHAR(255) NOT NULL,
    email            VARCHAR(100) NOT NULL UNIQUE,
    vai_tro          VARCHAR(20)  NOT NULL DEFAULT 'NGUOI_XEM',
    trang_thai       VARCHAR(20)  NOT NULL DEFAULT 'HOAT_DONG',
    CONSTRAINT ck_nguoi_dung_vai_tro   CHECK (vai_tro IN ('ADMIN', 'CHUYEN_GIA', 'NGUOI_XEM')),
    CONSTRAINT ck_nguoi_dung_trang_thai CHECK (trang_thai IN ('HOAT_DONG', 'KHOA'))
);

-- 2. DATASET
CREATE TABLE dataset (
    ma_dataset          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ma_nguoi_dung       BIGINT       NOT NULL,
    ten_dataset         VARCHAR(100) NOT NULL,
    mo_ta               TEXT,
    nguon_du_lieu       VARCHAR(50)  NOT NULL,
    thoi_gian_bat_dau   DATE,
    thoi_gian_ket_thuc  DATE,
    trang_thai          VARCHAR(20)  NOT NULL DEFAULT 'MOI_TAI',
    ngay_tao            TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_dataset_nguoi_dung FOREIGN KEY (ma_nguoi_dung)
        REFERENCES nguoi_dung (ma_nguoi_dung) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT ck_dataset_nguon CHECK (nguon_du_lieu IN ('Landsat', 'MODIS', 'Landsat_MODIS_Combined')),
    CONSTRAINT ck_dataset_thoi_gian CHECK (
        thoi_gian_bat_dau IS NULL OR thoi_gian_ket_thuc IS NULL
        OR thoi_gian_bat_dau <= thoi_gian_ket_thuc)
);
CREATE INDEX idx_dataset_nguoi_dung ON dataset (ma_nguoi_dung);

-- 3. KHU_VUC
-- Ranh giới TP.HCM sau sáp nhập + 168 đơn vị hành chính cấp xã (nạp từ SHP/GeoJSON
-- bằng backend_python/import_khu_vuc.py hoặc QGIS/ogr2ogr).
-- ma_khu_vuc: 'HCM' cho thành phố; đơn vị cấp xã: <LOAI>_<MA_DVHC>, vd 'PHUONG_26734'.
-- loai_khu_vuc: THANH_PHO | PHUONG | XA | DAC_KHU
CREATE TABLE khu_vuc (
    ma_khu_vuc      VARCHAR(50)  PRIMARY KEY,
    ma_hanh_chinh   VARCHAR(20)  UNIQUE,
    ten_khu_vuc     VARCHAR(255) NOT NULL,
    loai_khu_vuc    VARCHAR(50)  NOT NULL,
    ma_khu_vuc_cha  VARCHAR(50),
    dien_tich_km2   DOUBLE PRECISION,
    ranh_gioi       geometry(MultiPolygon, 4326),
    CONSTRAINT fk_khu_vuc_cha FOREIGN KEY (ma_khu_vuc_cha)
        REFERENCES khu_vuc (ma_khu_vuc) ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT ck_khu_vuc_khong_tu_cha CHECK (ma_khu_vuc_cha IS NULL OR ma_khu_vuc_cha <> ma_khu_vuc)
);
CREATE INDEX idx_khu_vuc_cha       ON khu_vuc (ma_khu_vuc_cha);
CREATE INDEX idx_khu_vuc_ranh_gioi ON khu_vuc USING GIST (ranh_gioi);

-- 4. LST_DATA
CREATE TABLE lst_data (
    ma_lst          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ma_dataset      BIGINT NOT NULL,
    thoi_gian       DATE   NOT NULL,
    vi_do           DOUBLE PRECISION NOT NULL,
    kinh_do         DOUBLE PRECISION NOT NULL,
    gia_tri_lst     DOUBLE PRECISION,
    ndvi            DOUBLE PRECISION,
    luong_mua       DOUBLE PRECISION,
    do_am_dat       DOUBLE PRECISION,
    lop_phu_be_mat  VARCHAR(50),
    geom            geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(kinh_do, vi_do), 4326)) STORED,
    CONSTRAINT fk_lst_data_dataset FOREIGN KEY (ma_dataset)
        REFERENCES dataset (ma_dataset) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT ck_lst_kinh_do CHECK (kinh_do BETWEEN -180 AND 180),
    CONSTRAINT ck_lst_vi_do   CHECK (vi_do   BETWEEN  -90 AND  90),
    CONSTRAINT ck_lst_ndvi    CHECK (ndvi IS NULL OR ndvi BETWEEN -1 AND 1),
    CONSTRAINT ck_lst_mua     CHECK (luong_mua IS NULL OR luong_mua >= 0),
    CONSTRAINT ck_lst_do_am   CHECK (do_am_dat IS NULL OR do_am_dat BETWEEN 0 AND 100),
    CONSTRAINT uq_lst_dataset_tg_vitri UNIQUE (ma_dataset, thoi_gian, kinh_do, vi_do)
);
CREATE INDEX idx_lst_dataset_tg ON lst_data (ma_dataset, thoi_gian);
CREATE INDEX idx_lst_geom       ON lst_data USING GIST (geom);

-- 5. TIEN_XU_LY
CREATE TABLE tien_xu_ly (
    ma_tien_xu_ly          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ma_dataset             BIGINT      NOT NULL,
    cach_xu_ly_thieu       VARCHAR(50),
    cach_xu_ly_nhieu       VARCHAR(50),
    phuong_phap_chuan_hoa  VARCHAR(50) NOT NULL DEFAULT 'Khong',
    ngay_ket_thuc_train    DATE        NOT NULL,
    ngay_ket_thuc_val      DATE,
    ngay_tao               TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_txl_dataset FOREIGN KEY (ma_dataset)
        REFERENCES dataset (ma_dataset) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT ck_txl_chuan_hoa CHECK (phuong_phap_chuan_hoa IN ('Min-Max', 'Z-score', 'Khong')),
    CONSTRAINT ck_txl_moc_chia  CHECK (ngay_ket_thuc_val IS NULL OR ngay_ket_thuc_train < ngay_ket_thuc_val)
);
CREATE INDEX idx_txl_dataset ON tien_xu_ly (ma_dataset);

-- 6. MODEL
CREATE TABLE model (
    ma_model    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ten_model   VARCHAR(50) NOT NULL UNIQUE,
    loai_model  VARCHAR(30) NOT NULL,
    mo_ta       TEXT,
    trang_thai  VARCHAR(20) NOT NULL DEFAULT 'HOAT_DONG',
    CONSTRAINT ck_model_loai CHECK (loai_model IN ('THONG_KE', 'HOC_MAY', 'HOC_SAU')),
    CONSTRAINT ck_model_trang_thai CHECK (trang_thai IN ('HOAT_DONG', 'NGUNG'))
);

-- 7. TRAINING_RUN
CREATE TABLE training_run (
    ma_training         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ma_dataset          BIGINT NOT NULL,
    ma_tien_xu_ly       BIGINT NOT NULL,
    ma_model            BIGINT NOT NULL,
    ma_nguoi_dung       BIGINT NOT NULL,
    thoi_gian_bat_dau   TIMESTAMP,
    thoi_gian_ket_thuc  TIMESTAMP,
    trang_thai          VARCHAR(20) NOT NULL DEFAULT 'DANG_CHAY',
    tham_so             JSONB,
    CONSTRAINT fk_tr_dataset    FOREIGN KEY (ma_dataset)    REFERENCES dataset (ma_dataset)         ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_tr_tien_xu_ly FOREIGN KEY (ma_tien_xu_ly) REFERENCES tien_xu_ly (ma_tien_xu_ly)   ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_tr_model      FOREIGN KEY (ma_model)      REFERENCES model (ma_model)             ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_tr_nguoi_dung FOREIGN KEY (ma_nguoi_dung) REFERENCES nguoi_dung (ma_nguoi_dung)   ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT ck_tr_trang_thai CHECK (trang_thai IN ('DANG_CHAY', 'HOAN_THANH', 'LOI')),
    CONSTRAINT ck_tr_thoi_gian  CHECK (
        thoi_gian_bat_dau IS NULL OR thoi_gian_ket_thuc IS NULL
        OR thoi_gian_bat_dau <= thoi_gian_ket_thuc)
);
CREATE INDEX idx_tr_dataset    ON training_run (ma_dataset);
CREATE INDEX idx_tr_tien_xu_ly ON training_run (ma_tien_xu_ly);
CREATE INDEX idx_tr_model      ON training_run (ma_model);
CREATE INDEX idx_tr_nguoi_dung ON training_run (ma_nguoi_dung);

-- 8. MODEL_METRIC
CREATE TABLE model_metric (
    ma_metric     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ma_training   BIGINT      NOT NULL,
    tap_danh_gia  VARCHAR(20) NOT NULL,
    mae           DOUBLE PRECISION,
    rmse          DOUBLE PRECISION,
    r2            DOUBLE PRECISION,
    mape          DOUBLE PRECISION,
    CONSTRAINT fk_metric_training FOREIGN KEY (ma_training)
        REFERENCES training_run (ma_training) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT ck_metric_tap CHECK (tap_danh_gia IN ('KIEM_DINH', 'KIEM_TRA')),
    CONSTRAINT ck_metric_gia_tri CHECK (
        (mae  IS NULL OR mae  >= 0) AND (rmse IS NULL OR rmse >= 0) AND
        (mape IS NULL OR mape >= 0) AND (r2   IS NULL OR r2   <= 1)),
    CONSTRAINT uq_metric_training_tap UNIQUE (ma_training, tap_danh_gia)
);
CREATE INDEX idx_metric_training ON model_metric (ma_training);

-- 9. FORECAST_RESULT
CREATE TABLE forecast_result (
    ma_du_bao           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ma_training         BIGINT NOT NULL,
    ma_khu_vuc          VARCHAR(50) NOT NULL,
    thoi_gian_du_bao    DATE   NOT NULL,
    vi_do               DOUBLE PRECISION NOT NULL,
    kinh_do             DOUBLE PRECISION NOT NULL,
    gia_tri_lst_du_bao  DOUBLE PRECISION NOT NULL,
    geom                geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(kinh_do, vi_do), 4326)) STORED,
    CONSTRAINT fk_fr_training FOREIGN KEY (ma_training) REFERENCES training_run (ma_training) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_fr_khu_vuc  FOREIGN KEY (ma_khu_vuc)  REFERENCES khu_vuc (ma_khu_vuc)       ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT ck_fr_kinh_do CHECK (kinh_do BETWEEN -180 AND 180),
    CONSTRAINT ck_fr_vi_do   CHECK (vi_do   BETWEEN  -90 AND  90),
    CONSTRAINT uq_fr_dubao UNIQUE (ma_training, ma_khu_vuc, thoi_gian_du_bao, kinh_do, vi_do)
);
CREATE INDEX idx_fr_training_tg ON forecast_result (ma_training, thoi_gian_du_bao);
CREATE INDEX idx_fr_khu_vuc     ON forecast_result (ma_khu_vuc);
CREATE INDEX idx_fr_geom        ON forecast_result USING GIST (geom);

-- 10. MAP_LAYER
CREATE TABLE map_layer (
    ma_layer           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ma_training        BIGINT       NOT NULL,
    ma_khu_vuc         VARCHAR(50)  NOT NULL,
    ten_layer          VARCHAR(100) NOT NULL,
    thoi_gian          DATE,
    duong_dan_du_lieu  VARCHAR(255),
    loai_du_lieu       VARCHAR(30),
    mo_ta              TEXT,
    CONSTRAINT fk_ml_training FOREIGN KEY (ma_training) REFERENCES training_run (ma_training) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_ml_khu_vuc  FOREIGN KEY (ma_khu_vuc)  REFERENCES khu_vuc (ma_khu_vuc)       ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE INDEX idx_ml_training ON map_layer (ma_training);
CREATE INDEX idx_ml_khu_vuc  ON map_layer (ma_khu_vuc);

-- VIEWS
CREATE OR REPLACE VIEW v_so_sanh_model AS
SELECT t.ma_training, m.ten_model, t.ma_dataset, t.tham_so,
       mm.mae, mm.rmse, mm.r2, mm.mape
FROM   training_run t
JOIN   model        m  ON m.ma_model = t.ma_model
JOIN   model_metric mm ON mm.ma_training = t.ma_training AND mm.tap_danh_gia = 'KIEM_TRA'
WHERE  t.trang_thai = 'HOAN_THANH'
ORDER  BY mm.rmse;

CREATE OR REPLACE VIEW v_du_bao_webgis AS
SELECT f.ma_du_bao, f.thoi_gian_du_bao, kv.ten_khu_vuc, m.ten_model,
       f.kinh_do, f.vi_do, f.gia_tri_lst_du_bao, f.geom
FROM   forecast_result f
JOIN   training_run t ON t.ma_training = f.ma_training
JOIN   model        m ON m.ma_model    = t.ma_model
JOIN   khu_vuc     kv ON kv.ma_khu_vuc = f.ma_khu_vuc;

-- =====================================================================
-- DỮ LIỆU BAN ĐẦU (SEED DATA)
-- =====================================================================

-- Người dùng
INSERT INTO nguoi_dung (ten_dang_nhap, mat_khau_ma_hoa, email, vai_tro, trang_thai) OVERRIDING SYSTEM VALUE VALUES
('admin', '$2b$10$wT8fH2p5vM7X.5pZfO1D5.lA9Tqj1K6vB3gZ9M.x0QY5uE.gM8iXe', 'admin@lst-hcm.gov.vn', 'ADMIN', 'HOAT_DONG'),
('expert_ai', '$2b$10$wT8fH2p5vM7X.5pZfO1D5.lA9Tqj1K6vB3gZ9M.x0QY5uE.gM8iXe', 'expert@lst-hcm.gov.vn', 'CHUYEN_GIA', 'HOAT_DONG'),
('viewer', '$2b$10$wT8fH2p5vM7X.5pZfO1D5.lA9Tqj1K6vB3gZ9M.x0QY5uE.gM8iXe', 'viewer@lst-hcm.gov.vn', 'NGUOI_XEM', 'HOAT_DONG');

-- Danh mục Mô hình AI
INSERT INTO model (ten_model, loai_model, mo_ta, trang_thai) OVERRIDING SYSTEM VALUE VALUES
('ARIMA', 'THONG_KE', 'Mô hình tự hồi quy tích hợp trung bình trượt phân tích chuỗi thời gian đơn biến', 'HOAT_DONG'),
('Random Forest', 'HOC_MAY', 'Mô hình rừng cây quyết định ngẫu nhiên dự báo phi tuyến đa biến', 'HOAT_DONG'),
('XGBoost', 'HOC_MAY', 'Mô hình tăng cường độ dốc Extreme Gradient Boosting trên dữ liệu bảng', 'HOAT_DONG'),
('GCN-LSTM', 'HOC_SAU', 'Mạng kết hợp đồ thị tích chập không gian và bộ nhớ dài ngắn hạn theo chuỗi thời gian', 'HOAT_DONG');

-- Khu vực: chỉ tạo bản ghi gốc 'HCM' với ranh giới tạm (hình chữ nhật).
-- Ranh giới thật + 168 đơn vị cấp xã nạp bằng: python import_khu_vuc.py <file.geojson>
-- (script sẽ thay ranh giới 'HCM' bằng phần hợp nhất của các xã/phường).
INSERT INTO khu_vuc (ma_khu_vuc, ma_hanh_chinh, ten_khu_vuc, loai_khu_vuc, ma_khu_vuc_cha, ranh_gioi) VALUES
('HCM', '79', 'Thành phố Hồ Chí Minh', 'THANH_PHO', NULL,
 ST_Multi(ST_GeomFromText('POLYGON((106.35 10.35, 107.03 10.35, 107.03 11.16, 106.35 11.16, 106.35 10.35))', 4326)));

-- Dataset mẫu
INSERT INTO dataset (ma_nguoi_dung, ten_dataset, mo_ta, nguon_du_lieu, thoi_gian_bat_dau, thoi_gian_ket_thuc, trang_thai) VALUES
(1, 'LST_TPHCM_Landsat_MODIS_2020_2025', 'Bộ dữ liệu LST chuẩn hóa từ vệ tinh Landsat 8/9 và MODIS giai đoạn 2020-2025 tại TP.HCM', 'Landsat', '2020-01-01', '2025-12-31', 'DA_XU_LY');

-- Cấu hình tiền xử lý mẫu
INSERT INTO tien_xu_ly (ma_dataset, cach_xu_ly_thieu, cach_xu_ly_nhieu, phuong_phap_chuan_hoa, ngay_ket_thuc_train, ngay_ket_thuc_val) VALUES
(1, 'Nội suy tuyến tính theo thời gian', 'Lọc ngưỡng IQR và phân vị [0.01, 0.99]', 'Min-Max', '2023-12-31', '2024-12-31');
