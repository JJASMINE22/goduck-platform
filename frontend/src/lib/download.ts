/* 浏览器下载触发（URL 形态）—— 用临时 <a> 点击触发导航；
   服务端 Content-Disposition: attachment 会让浏览器下载而非跳转，
   避免 window.open 留下空白标签页。文件名由服务端 RFC5987 决定。 */
export function downloadUrl(url: string): void {
  const a = document.createElement('a')
  a.href = url
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}
