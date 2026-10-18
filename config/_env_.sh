# 这个文件是文档,不是用来执行的
exit 0
# Haskell 环境更新
cabal update
# GHCup 安装和管理
#   ├── GHC        <- 真正编译 Haskell 的编译器
#   ├── cabal      <- 构建项目,管理依赖
#   └── HLS        <- 编辑器语言服务器(可选)

# 查看版本
cabal --version
ghc --version

# Rust 环境更新
rustup update
# 查看版本
cargo --version
rustc --version
rustup --version
