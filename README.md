# i过日子

个人项目单页站点，用于 GitHub Pages 托管。

## 部署到 GitHub Pages（3 分钟）

1. 新建仓库，命名随意，比如 `iguo-rizi`，**Public**
2. 把 `index.html` 传到仓库根目录
3. 仓库 → Settings → Pages
   - Source 选 `Deploy from a branch`
   - Branch 选 `main`，目录选 `/ (root)`
   - 保存
4. 等 1~2 分钟，访问：

```
https://<你的用户名>.github.io/iguo-rizi/
```

这个地址就是填到拼多多「官网地址」里的 URL。

## 审核前自查

- [ ] 地址能打开，不是 404
- [ ] 页面内容与「应用说明」一致（都讲"个人自用、商品信息参考"）
- [ ] 没有出现"比价""导购""返利""全网最低"等词
- [ ] 没有电商平台 logo 或品牌名
- [ ] 页脚有"个人项目、非商业、无隶属关系"声明

## 如果被拒

多半是「未备案」或「网站无实质内容」。两条路：

1. 换国内托管 + ICP 备案（成本高、周期长，不推荐个人做）
2. 放弃拼多多，转京东联盟「导购媒体」类型（不需要网站）

## 本地预览

直接双击 `index.html`，或：

```bash
python -m http.server 8080
```

打开 http://localhost:8080
