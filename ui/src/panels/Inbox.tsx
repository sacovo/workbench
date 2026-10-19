import { openItem, useStore, WHOSE, type ItemState } from "../store";

const GROUPS: ItemState["status"][] = ["waiting_owner", "waiting_agent", "done", "dropped"];
const CLOSED: ItemState["status"][] = ["done", "dropped"]; // folded at first: hundreds of rows nobody waits on

function Row({ item }: { item: ItemState }) {
    const unread = item.unread_owner.length > 0;
    const open = item.questions ? `${item.open_questions.length}/${item.questions.length} open` : null;
    return (
        <li className={unread ? "row unread" : "row"} data-item={item.id} onClick={() => openItem(item.id)}>
            <span className={unread ? "dot" : "dot none"} title={unread ? "new since your last action" : undefined} />
            <span className="id">{item.id}</span>
            <span className="kind">{item.kind}</span>
            <span className="title">{item.title}</span>
            <span className="dim open-count">{open}</span>
        </li>
    );
}

export function InboxPanel() {
    const { review } = useStore();
    if (!review.items.length && !review.unreadable.length) return <p className="dim">No review items yet.</p>;
    return (
        <div className="inbox">
            {GROUPS.map((status) => {
                const items = review.items.filter((i) => i.status === status).reverse();
                if (!items.length) return null;
                return (
                    <details key={status} data-group={status} open={!CLOSED.includes(status)}>
                        <summary>
                            {WHOSE[status]} <span className="dim">({items.length})</span>
                        </summary>
                        <ul>
                            {items.map((item) => (
                                <Row key={item.id} item={item} />
                            ))}
                        </ul>
                    </details>
                );
            })}
            {review.unreadable.length > 0 && (
                <section>
                    <h3>Unreadable</h3>
                    <ul>
                        {review.unreadable.map((u) => (
                            <li key={u.file} className="row error">
                                {u.file}: {u.error}
                            </li>
                        ))}
                    </ul>
                </section>
            )}
        </div>
    );
}
