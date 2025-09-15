import datetime
import functools
import re

from flask import (
    Blueprint,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from peewee import JOIN, IntegrityError, fn

from ..models import (
    Channel,
    ChannelMember,
    Conversation,
    Hashtag,
    Message,
    MessageHashtag,
    UploadedFile,
    User,
    UserConversationStatus,
    Workspace,
    WorkspaceMember,
    db,
)

admin_bp = Blueprint("admin", __name__, template_folder="../templates/admin")


def admin_required(view):
    """Decorator to ensure the user is logged in and is an admin."""

    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("main.login_page"))

        # Check for admin role in the workspace
        workspace_member = WorkspaceMember.get_or_none(user=g.user)
        if not workspace_member or workspace_member.role != "admin":
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for("main.chat_interface"))

        return view(**kwargs)

    return wrapped_view


@admin_bp.route("/")
@admin_required
def dashboard():
    """Renders the main admin dashboard with statistics."""
    # --- Stat card queries remain the same ---
    total_users = User.select().count()
    total_messages = Message.select().count()
    total_files = UploadedFile.select().count()
    total_channels = Channel.select().count()
    total_storage_bytes = (
        UploadedFile.select(fn.SUM(UploadedFile.file_size_bytes)).scalar() or 0
    )
    seven_days_ago_for_avg = datetime.datetime.now() - datetime.timedelta(days=7)
    messages_last_7_days = (
        Message.select().where(Message.created_at > seven_days_ago_for_avg).count()
    )
    total_seconds_in_7_days = 7 * 24 * 60 * 60
    avg_mps = (
        messages_last_7_days / total_seconds_in_7_days
        if messages_last_7_days > 0
        else 0
    )

    # --- [START OF NEW CHART LOGIC] ---
    # 1. Define the time window for the chart.
    now = datetime.datetime.now()
    twenty_four_hours_ago = now - datetime.timedelta(hours=24)

    # 2. Initialize a dictionary to hold message counts for every hour in the last 24 hours.
    #    We start with 0 for each hour to ensure all hours are represented on the chart.
    chart_data = {}
    for i in range(24):
        # We go back in time hour by hour from the current hour.
        hour_timestamp = (now - datetime.timedelta(hours=i)).replace(
            minute=0, second=0, microsecond=0
        )
        chart_data[hour_timestamp] = 0

    # 3. Query the database to get actual message counts grouped by hour.
    #    This STRFTIME format is compatible with both PostgreSQL and SQLite.
    query = (
        Message.select(
            fn.date_trunc("hour", Message.created_at).alias("hour"),
            fn.COUNT(Message.id).alias("count"),
        )
        .where(Message.created_at >= twenty_four_hours_ago)
        .group_by(fn.date_trunc("hour", Message.created_at))
    )

    # 4. Populate our dictionary with the real data from the query.
    for result in query:
        # The result.hour will now be a proper datetime object, so no conversion is needed.
        db_hour = result.hour
        if db_hour in chart_data:
            chart_data[db_hour] = result.count

    # 5. Sort the data by hour and prepare labels and values for Chart.js.
    sorted_chart_data = sorted(chart_data.items())

    # Format the hour labels for readability (e.g., "11 PM", "12 AM", "Now")
    chart_labels = []
    for hour, count in sorted_chart_data:
        if hour.hour == now.hour and hour.day == now.day:
            chart_labels.append("Now")
        else:
            chart_labels.append(
                hour.strftime("%-I %p").strip()
            )  # Use '%#I' on Windows if this fails

    chart_values = [count for hour, count in sorted_chart_data]
    # --- [END OF NEW CHART LOGIC] ---

    # Render the appropriate template based on the request type.
    template_name = (
        "admin/dashboard_content.html"
        if "HX-Request" in request.headers
        else "admin/dashboard.html"
    )

    return render_template(
        template_name,
        total_users=total_users,
        total_messages=total_messages,
        total_files=total_files,
        total_channels=total_channels,
        total_storage_bytes=total_storage_bytes,
        avg_mps=avg_mps,
        chart_labels=chart_labels,
        chart_values=chart_values,
    )


@admin_bp.route("/users")
@admin_required
def list_users():
    """Lists all users with their workspace role."""
    # Query to join User and WorkspaceMember to get the role
    users_with_roles = (
        User.select(User, WorkspaceMember.role)
        .join(WorkspaceMember, on=(User.id == WorkspaceMember.user))
        .order_by(User.id)
    )

    # handle the HTMX request
    if "HX-Request" in request.headers:
        return render_template("admin/user_list_content.html", users=users_with_roles)

    return render_template("user_list.html", users=users_with_roles)


@admin_bp.route("/users/create", methods=["GET"])
@admin_required
def create_user_form():
    return render_template("admin/create_user_content.html")


@admin_bp.route("/users/create", methods=["POST"])
@admin_required
def create_user():
    """Creates a new local user with a password and role."""
    username = request.form.get("username")
    email = request.form.get("email")
    password = request.form.get("password")
    role = request.form.get("role", "member")  # Default to 'member'
    display_name = request.form.get("display_name")

    if not all([username, email, password]):
        flash("Username, email, and password are required.", "danger")
        return render_template("admin/create_user_content.html")

    try:
        with db.atomic():
            # Create the User
            new_user = User(
                username=username,
                email=email,
                display_name=display_name,
                last_threads_view_at=datetime.datetime.now(),
            )
            new_user.set_password(password)
            new_user.save()

            # Add them to the primary workspace
            workspace = Workspace.get(id=1)
            WorkspaceMember.create(user=new_user, workspace=workspace, role=role)

            # Add them to the 'general' and 'announcements' channels
            general = Channel.get(Channel.name == "general")
            announcements = Channel.get(Channel.name == "announcements")
            ChannelMember.create(user=new_user, channel=general)
            ChannelMember.create(user=new_user, channel=announcements)

            flash(f"User '{username}' created successfully.", "success")
    except IntegrityError:
        flash(f"Username or email '{username}' already exists.", "danger")
        return render_template("admin/create_user_content.html")

    # get the new users list so we can show them the current one
    users_with_roles = (
        User.select(User, WorkspaceMember.role)
        .join(WorkspaceMember, on=(User.id == WorkspaceMember.user))
        .order_by(User.id)
    )
    # The target is #admin-content, so we render the content partial.
    return render_template("admin/user_list_content.html", users=users_with_roles)


@admin_bp.route("/users/edit/<int:user_id>", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    """Handles both displaying and processing the user edit form."""
    user = User.get_or_none(id=user_id)
    if not user:
        flash("User not found.", "danger")
        if "HX-Request" in request.headers:
            response = make_response()
            response.headers["HX-Redirect"] = url_for("admin.list_users")
            return response
        return redirect(url_for("admin.list_users"))

    workspace_member = WorkspaceMember.get(user=user)

    if request.method == "POST":
        # Process the form submission
        user.username = request.form.get("username")
        user.display_name = request.form.get("display_name")
        user.email = request.form.get("email")
        workspace_member.role = request.form.get("role")

        new_password = request.form.get("password")
        if new_password:
            user.set_password(new_password)

        try:
            with db.atomic():
                user.save()
                workspace_member.save()
            flash(f"User '{user.username}' updated successfully.", "success")
        except IntegrityError:
            flash("Username or email already exists.", "danger")
            # Don't redirect, so the admin can fix the error
            return render_template(
                "admin/edit_user_content.html",
                user=user,
                workspace_member=workspace_member,
            )

        response = make_response()
        response.headers["HX-Redirect"] = url_for("admin.list_users")
        return response

    # For a GET request, just show the form
    template_name = (
        "admin/edit_user_content.html"
        if "HX-Request" in request.headers
        else "admin/edit_user.html"
    )
    return render_template(template_name, user=user, workspace_member=workspace_member)


# --- Channel Management Routes ---
@admin_bp.route("/channels")
@admin_required
def list_channels():
    """Displays a list of all channels with their member counts."""
    # This query joins Channel with ChannelMember and groups by the channel
    # to calculate the number of members for each one.
    channels_with_counts = (
        Channel.select(Channel, User, fn.COUNT(ChannelMember.id).alias("member_count"))
        .join(User, on=(Channel.created_by == User.id), join_type=JOIN.LEFT_OUTER)
        .switch(Channel)
        .join(
            ChannelMember,
            on=(Channel.id == ChannelMember.channel),
            join_type=JOIN.LEFT_OUTER,
        )
        .group_by(Channel.id, User.id)
        .order_by(Channel.name)
    )

    # Handle HTMX requests
    if "HX-Request" in request.headers:
        return render_template(
            "admin/channel_list_content.html", channels=channels_with_counts
        )

    return render_template("channel_list.html", channels=channels_with_counts)


@admin_bp.route("/channels/create", methods=["GET", "POST"])
@admin_required
def create_channel():
    """Handles the creation of a new channel from the admin panel."""
    if request.method == "POST":
        name = request.form.get("name", "").strip().lower()
        name = re.sub(r"[^a-zA-Z0-9_-]", "", name)  # Sanitize the name
        topic = request.form.get("topic", "").strip()
        description = request.form.get("description", "").strip()
        is_private = request.form.get("is_private") == "on"

        if not name or len(name) < 3:
            flash(
                "Name must be at least 3 characters and contain only letters, numbers, underscores, or hyphens.",
                "danger",
            )
            return redirect(url_for("admin.create_channel"))

        try:
            with db.atomic():
                workspace = Workspace.get(id=1)  # Assuming a single workspace
                Channel.create(
                    workspace=workspace,
                    name=name,
                    topic=topic,
                    description=description,
                    is_private=is_private,
                    created_by=g.user,
                )

                # Clean up any existing hashtags that match the new channel name.
                hashtag_to_delete = Hashtag.get_or_none(name=name)
                if hashtag_to_delete:
                    MessageHashtag.delete().where(
                        MessageHashtag.hashtag == hashtag_to_delete
                    ).execute()
                    hashtag_to_delete.delete_instance()

            flash(f"Channel '#{name}' created successfully.", "success")
            return redirect(url_for("admin.list_channels"))
        except IntegrityError:
            flash(f"A channel with the name '#{name}' already exists.", "danger")
            return redirect(url_for("admin.create_channel"))

    # For a GET request, just render the form
    return render_template("create_channel.html")


@admin_bp.route("/channels/edit/<int:channel_id>", methods=["GET", "POST"])
@admin_required
def edit_channel(channel_id):
    """Handles editing a channel's details."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        flash("Channel not found.", "danger")
        return redirect(url_for("admin.list_channels"))

    if request.method == "POST":
        # Prevent changing the name of default channels
        if channel.name not in ["general", "announcements"]:
            name = request.form.get("name", "").strip().lower()
            name = re.sub(r"[^a-zA-Z0-9_-]", "", name)
            if not name or len(name) < 3:
                flash(
                    "Name must be at least 3 characters and contain only letters, numbers, underscores, or hyphens.",
                    "danger",
                )
                return redirect(url_for("admin.edit_channel", channel_id=channel_id))
            channel.name = name

        channel.topic = request.form.get("topic", "").strip()
        channel.description = request.form.get("description", "").strip()
        channel.is_private = request.form.get("is_private") == "on"

        try:
            channel.save()
            flash(f"Channel '#{channel.name}' updated successfully.", "success")
            return redirect(url_for("admin.list_channels"))
        except IntegrityError:
            flash("A channel with that name already exists.", "danger")
            return redirect(url_for("admin.edit_channel", channel_id=channel_id))

    # GET Request
    # Get a list of user IDs already in the channel
    current_member_ids = [
        member.user.id
        for member in ChannelMember.select().where(ChannelMember.channel == channel)
    ]

    # Get all members of the channel, joining the User data
    current_members = (
        ChannelMember.select(ChannelMember, User)
        .join(User)
        .where(ChannelMember.channel == channel)
        .order_by(User.username)
    )

    # Get users who are in the workspace but NOT in the current channel
    users_to_add = (
        User.select()
        .join(WorkspaceMember)
        .where(
            (WorkspaceMember.workspace == channel.workspace)
            & (User.id.not_in(current_member_ids))
        )
        .order_by(User.username)
    )

    return render_template(
        "edit_channel.html",
        channel=channel,
        current_members=current_members,
        users_to_add=users_to_add,
    )


@admin_bp.route("/channels/<int:channel_id>/members/add", methods=["POST"])
@admin_required
def admin_add_channel_member(channel_id):
    """Adds a user to a channel from the admin panel."""
    channel = Channel.get_or_none(id=channel_id)
    user_id_to_add = request.form.get("user_id", type=int)

    if not channel or not user_id_to_add:
        flash("Invalid channel or user specified.", "danger")
        return redirect(url_for("admin.edit_channel", channel_id=channel_id))

    # Check if the user is already a member
    is_member = (
        ChannelMember.select()
        .where(
            (ChannelMember.channel == channel) & (ChannelMember.user == user_id_to_add)
        )
        .exists()
    )

    if not is_member:
        # Also ensure we create the conversation status so the user sees the channel in the app
        conversation, _ = Conversation.get_or_create(
            conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
        )
        UserConversationStatus.get_or_create(
            user_id=user_id_to_add, conversation=conversation
        )

        ChannelMember.create(user=user_id_to_add, channel=channel)
        flash("User added to channel successfully.", "success")
    else:
        flash("User is already a member of this channel.", "warning")

    return redirect(url_for("admin.edit_channel", channel_id=channel_id))


@admin_bp.route(
    "/channels/<int:channel_id>/members/<int:user_id>/remove", methods=["POST"]
)
@admin_required
def remove_channel_member(channel_id, user_id):
    """Removes a user from a channel."""
    channel = Channel.get_or_none(id=channel_id)
    user_to_remove = User.get_or_none(id=user_id)

    if not channel or not user_to_remove:
        flash("Invalid channel or user specified.", "danger")
        return redirect(url_for("admin.list_channels"))

    membership = ChannelMember.get_or_none(user=user_to_remove, channel=channel)
    if not membership:
        flash("User is not a member of this channel.", "warning")
        return redirect(url_for("admin.edit_channel", channel_id=channel_id))

    # Safety check: Prevent removing the last admin if there are other members
    member_count = (
        ChannelMember.select().where(ChannelMember.channel == channel).count()
    )
    if membership.role == "admin" and member_count > 1:
        admin_count = (
            ChannelMember.select()
            .where((ChannelMember.channel == channel) & (ChannelMember.role == "admin"))
            .count()
        )
        if admin_count <= 1:
            flash(
                "You cannot remove the last admin from a channel with other members.",
                "danger",
            )
            return redirect(url_for("admin.edit_channel", channel_id=channel_id))

    membership.delete_instance()
    flash("User removed from channel successfully.", "success")
    return redirect(url_for("admin.edit_channel", channel_id=channel_id))


@admin_bp.route(
    "/channels/<int:channel_id>/members/<int:user_id>/role", methods=["POST"]
)
@admin_required
def update_member_role(channel_id, user_id):
    """Updates a user's role within a channel."""
    channel = Channel.get_or_none(id=channel_id)
    user_to_update = User.get_or_none(id=user_id)
    new_role = request.form.get("role")

    if not all([channel, user_to_update, new_role in ["admin", "member"]]):
        flash("Invalid request parameters.", "danger")
        return redirect(url_for("admin.edit_channel", channel_id=channel_id))

    membership = ChannelMember.get_or_none(user=user_to_update, channel=channel)
    if not membership:
        flash("User is not a member of this channel.", "warning")
        return redirect(url_for("admin.edit_channel", channel_id=channel_id))

    # Safety check: Prevent demoting the last admin if other members exist
    if membership.role == "admin" and new_role == "member":
        member_count = (
            ChannelMember.select().where(ChannelMember.channel == channel).count()
        )
        admin_count = (
            ChannelMember.select()
            .where((ChannelMember.channel == channel) & (ChannelMember.role == "admin"))
            .count()
        )
        if admin_count <= 1 and member_count > 1:
            flash(
                "Cannot demote the last admin when other members are present.", "danger"
            )
            return redirect(url_for("admin.edit_channel", channel_id=channel_id))

    membership.role = new_role
    membership.save()
    flash(f"{user_to_update.username}'s role updated to {new_role}.", "success")

    return redirect(url_for("admin.edit_channel", channel_id=channel_id))
