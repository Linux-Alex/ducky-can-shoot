import QtQuick
import org.kde.kirigami as Kirigami

/*!
    List delegate for the stages model.

    The required properties are the model roles (QML populates them by name);
    the visible row is a StageRow fed explicitly from this wrapper, which keeps
    the role names and the component's own property names from colliding.
*/
Item {
    id: wrapper

    required property string stageId
    required property string title
    required property string description
    required property string icon
    required property string state
    required property real percent
    required property string message
    required property string current
    required property real elapsed
    required property real eta
    required property real rate
    required property int stageIndex

    implicitHeight: row.implicitHeight

    function formatDuration(seconds) {
        if (!Number.isFinite(seconds) || seconds <= 0) {
            return "";
        }
        const total = Math.round(seconds);
        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const secs = total % 60;
        const pad = n => (n < 10 ? "0" + n : "" + n);
        return hours > 0 ? hours + ":" + pad(minutes) + ":" + pad(secs) : pad(minutes) + ":" + pad(secs);
    }

    StageRow {
        id: row
        anchors.left: parent.left
        anchors.right: parent.right
        stageId: wrapper.stageId
        title: wrapper.title
        description: wrapper.description
        iconName: wrapper.icon
        state: wrapper.state
        percent: wrapper.percent
        message: wrapper.message
        current: wrapper.current
        elapsedText: wrapper.formatDuration(wrapper.elapsed)
        etaText: wrapper.eta >= 0 ? wrapper.formatDuration(wrapper.eta) : ""
        rateText: wrapper.rate > 0 ? formatRate(wrapper.rate) : ""
        stageIndex: wrapper.stageIndex

        function formatRate(bytesPerSecond) {
            const units = ["B", "KiB", "MiB", "GiB"];
            let value = bytesPerSecond;
            let unit = 0;
            while (value >= 1024 && unit < units.length - 1) {
                value /= 1024;
                ++unit;
            }
            return (Math.round(value * 10) / 10) + " " + units[unit] + "/s";
        }
    }
}
