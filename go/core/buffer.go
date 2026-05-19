package core

// Buffer defines the storage interface for tasks and events.
// It is implemented by transport.SQLiteBuffer and can be satisfied
// by any in-memory mock for testing.
type Buffer interface {
	InsertTask(task Task) error
	UpdateTask(task Task) error
	GetTask(taskID string) (*Task, error)
	InsertEvent(event Event) error
	UpdateEvent(event Event) error
	QueryEvents(taskID string) ([]Event, error)
	QueryPendingEvents(limit int) ([]Event, error)
	MarkSynced(eventIDs []string) error
	Close() error
}

// TransactionalBuffer extends Buffer with atomic multi-operation support.
// Implementations that support database transactions (e.g. SQLiteBuffer)
// can satisfy this interface to allow callers to insert a task and its
// events in a single atomic operation.
type TransactionalBuffer interface {
	Buffer
	InsertTaskWithEvents(task Task, events []Event) error
}
