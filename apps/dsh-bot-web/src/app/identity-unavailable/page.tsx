export const dynamic = "force-dynamic";

export default function IdentityUnavailablePage() {
  return (
    <main style={{ padding: 24 }}>
      <h1>503</h1>
      <p>生产身份失败关闭：未配置 IAP issuer / audience / JWKS，或 JWKS 地址不被允许。</p>
    </main>
  );
}
