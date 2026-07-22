/* 公共原始类型 —— 对应后端 uuid.UUID / datetime 等（schemas.py）。
   品牌类型让语义更清晰，运行时仍是普通 string。 */

/** 后端 UUID（gen_random_uuid()），前端统一按字符串处理 */
export type UUID = string

/** 后端 TIMESTAMPTZ，ISO 8601 字符串 */
export type ISODateTime = string
