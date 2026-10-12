extends CharacterBody2D
## Parameters are supplied by the validated prototype compiler.
const SPEED := __SPEED__.0
const JUMP_VELOCITY := -__JUMP__.0
const MAX_JUMPS := __JUMPS__
var jumps_used := 0
var gravity: float = ProjectSettings.get_setting("physics/2d/default_gravity", 980.0)

func _physics_process(delta: float) -> void:
    if is_on_floor():
        jumps_used = 0
    else:
        velocity.y += gravity * delta
    if Input.is_action_just_pressed("ui_accept") and jumps_used < MAX_JUMPS:
        # Walking off a ledge consumes the ground jump.
        if not is_on_floor() and jumps_used == 0:
            jumps_used = 1
        if jumps_used < MAX_JUMPS:
            velocity.y = JUMP_VELOCITY
            jumps_used += 1
    velocity.x = Input.get_axis("ui_left", "ui_right") * SPEED
    move_and_slide()
