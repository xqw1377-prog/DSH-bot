export const dynamic = "force-dynamic";

export default function UnauthenticatedPage() {
  return (
    <main style={{ padding: 24 }}>
      <h1>401</h1>
      <p>需要 Viewer Principal。请通过 IAP 登录后再打开控制台。</p>
    </main>
  );
}
