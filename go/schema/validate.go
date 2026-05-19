package schema

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v5"
)

//go:embed dexcost-event.v1.json
var eventSchemaJSON string

//go:embed dexcost-task.v1.json
var taskSchemaJSON string

var (
	eventSchema *jsonschema.Schema
	taskSchema  *jsonschema.Schema
)

func init() {
	compiler := jsonschema.NewCompiler()
	compiler.Draft = jsonschema.Draft7

	if err := compiler.AddResource("event.json", strings.NewReader(eventSchemaJSON)); err != nil {
		panic(err)
	}
	if err := compiler.AddResource("task.json", strings.NewReader(taskSchemaJSON)); err != nil {
		panic(err)
	}
	var err error
	eventSchema, err = compiler.Compile("event.json")
	if err != nil {
		log.Printf("[dexcost] failed to compile event schema: %v", err)
	}
	taskSchema, err = compiler.Compile("task.json")
	if err != nil {
		log.Printf("[dexcost] failed to compile task schema: %v", err)
	}
}

// Validate checks a task or event payload against Schema v1.
// Returns nil on success, or a slice of error strings.
func Validate(payload map[string]interface{}) []string {
	sv, _ := payload["schema_version"].(string)
	if sv == "" {
		sv = "1"
	}
	if sv != "1" {
		return []string{fmt.Sprintf("Unsupported schema_version: %s", sv)}
	}

	_, hasEventID := payload["event_id"]
	_, hasTaskID := payload["task_id"]

	var schema *jsonschema.Schema
	if hasEventID {
		schema = eventSchema
	} else if hasTaskID {
		schema = taskSchema
	} else {
		return []string{"Cannot determine payload type: missing task_id or event_id"}
	}

	if schema == nil {
		return nil // Schema not loaded — can't validate
	}

	// Convert to JSON and back for validation
	data, err := json.Marshal(payload)
	if err != nil {
		return []string{fmt.Sprintf("failed to marshal payload: %v", err)}
	}
	var v interface{}
	if umErr := json.Unmarshal(data, &v); umErr != nil {
		return []string{fmt.Sprintf("failed to unmarshal payload: %v", umErr)}
	}

	err = schema.Validate(v)
	if err == nil {
		return nil
	}

	// Extract validation errors
	var errors []string
	if ve, ok := err.(*jsonschema.ValidationError); ok {
		for _, cause := range ve.Causes {
			errors = append(errors, fmt.Sprintf("%s: %s", cause.InstanceLocation, cause.Message))
		}
		if len(errors) == 0 {
			errors = append(errors, ve.Message)
		}
	} else {
		errors = append(errors, err.Error())
	}
	return errors
}
