import Link from "next/link";
import type { TodayBoard, TodayStory } from "@dsh-bot/client-sdk";

export function TodayBoardView({ today }: { today: TodayBoard }) {
  return (
    <section data-testid="today-board" style={{ marginBottom: 24 }}>
      <h2 style={{ margin: "0 0 12px", fontSize: 22 }}>{today.headline}</h2>
      <div
        style={{
          display: "grid",
          gap: 16,
          gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
        }}
      >
        {today.stories.map((story) => (
          <StoryCard key={story.market} story={story} />
        ))}
      </div>
      {(today.attention || []).length > 0 && (
        <section
          style={{
            marginTop: 16,
            padding: 16,
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            backgroundColor: "#ffffff",
          }}
        >
          <h3 style={{ margin: "0 0 8px", fontSize: 16 }}>今天需要你关注的五件事</h3>
          <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>
            {(today.attention || []).map((row, index) => (
              <li key={`${String(row.market || "GLOBAL")}-${String(row.symbol || index)}`}>
                {String(row.market || "GLOBAL")} {String(row.symbol || "")} {String(row.action || "WATCH")} ·{" "}
                {String(row.title || "")}
              </li>
            ))}
          </ul>
        </section>
      )}
      <p style={{ margin: "12px 0 0", fontSize: 13, color: "#6b7280" }}>
        {today.disclaimer} <Link href="/shadow">全部决策</Link>
      </p>
    </section>
  );
}

function StoryCard({ story }: { story: TodayStory }) {
  return (
    <article
      data-testid={`today-story-${story.market}`}
      style={{
        padding: 16,
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        backgroundColor: "#ffffff",
      }}
    >
      <h3 style={{ margin: "0 0 8px", fontSize: 16 }}>{story.title}</h3>
      <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>
        {story.points.map((point) => (
          <li key={point}>{point}</li>
        ))}
      </ul>
    </article>
  );
}
