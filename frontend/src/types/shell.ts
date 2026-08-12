/* Shell 执行能力类型 —— 与后端 models/schemas.py (ShellCapability) +
   api/stream.py (GET /sessions/{id}/shell-capability) 1:1 对齐。

   allow_shell = 全局 enable_shell AND 主代理配置 allow_shell=true。
   驱动会话页 shell 能力徽标可见性（准则 F2 能力驱动 UI）。
   注意：本地 shell 非安全边界（Route B），仅单信任域部署。 */

/** GET /sessions/{id}/shell-capability —— 驱动会话页 shell 能力徽标（准则 F2） */
export interface ShellCapability {
  allow_shell: boolean
}
